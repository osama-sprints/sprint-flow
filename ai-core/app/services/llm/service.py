"""LLM service with retries, circular fallback, per-call tool binding and optional structured output."""

import asyncio
import logging
from typing import (
    Any,
    Callable,
    List,
    Optional,
    Sequence,
    Type,
    TypeVar,
    Union,
    overload,
)

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import BaseMessage
from openai import (
    APIError,
    APITimeoutError,
    OpenAIError,
    RateLimitError,
)
from pydantic import BaseModel
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings
from app.core.logging import logger
from app.services.llm.registry import LLMRegistry

T = TypeVar("T", bound=BaseModel)


class LLMService:
    """Service for managing LLM calls with retries and circular fallback.

    Three execution paths:

    - **Default path** (no overrides, no ``tools``): uses ``self._llm``, the
      model with the default tool set bound. Circular fallback re-binds the
      default tools on the next model so bindings survive a switch.

    - **Specialist path** (``tools`` given): binds exactly that tool subset to
      the current base model for this call only. This is how each supervisor
      route gets a genuinely different capability set without a second service.

    - **One-off path** (any of model_name / response_format / model_kwargs):
      resolves a fresh, local ``Runnable`` without touching ``self._llm``.
    """

    def __init__(self):
        """Initialize the LLM service with the configured default model."""
        self._base_llm: Any = None  # the current model, never tool-bound
        self._llm: Any = None  # the current model with the default tools bound
        self._current_model_index: int = 0
        self._bound_tools: List = []

        all_names = LLMRegistry.get_all_names()
        try:
            self._current_model_index = all_names.index(settings.DEFAULT_LLM_MODEL)
            self._base_llm = LLMRegistry.get(settings.DEFAULT_LLM_MODEL)
            logger.info(
                "llm_service_initialized",
                default_model=settings.DEFAULT_LLM_MODEL,
                model_index=self._current_model_index,
                total_models=len(all_names),
                environment=settings.ENVIRONMENT.value,
            )
        except Exception as e:
            self._current_model_index = 0
            self._base_llm = LLMRegistry.LLMS[0]["llm"]
            logger.warning(
                "default_model_not_found_using_first",
                requested=settings.DEFAULT_LLM_MODEL,
                using=all_names[0] if all_names else "none",
                error=str(e),
            )
        self._llm = self._base_llm

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @overload
    async def call(
        self,
        messages: LanguageModelInput,
        model_name: Optional[str] = ...,
        response_format: None = ...,
        *,
        tools: Optional[Sequence[Any]] = ...,
        tool_choice: Optional[str] = ...,
        **model_kwargs: Any,
    ) -> BaseMessage: ...

    @overload
    async def call(
        self,
        messages: LanguageModelInput,
        model_name: Optional[str] = ...,
        *,
        response_format: Type[T],
        tools: Optional[Sequence[Any]] = ...,
        tool_choice: Optional[str] = ...,
        **model_kwargs: Any,
    ) -> T: ...

    async def call(
        self,
        messages: LanguageModelInput,
        model_name: Optional[str] = None,
        response_format: Optional[Type[BaseModel]] = None,
        *,
        tools: Optional[Sequence[Any]] = None,
        tool_choice: Optional[str] = None,
        **model_kwargs: Any,
    ) -> Union[BaseMessage, BaseModel]:
        """Call the LLM with retries and circular fallback.

        Args:
            messages: Conversation messages to send.
            model_name: Override the model. ``None`` uses the current default.
            response_format: Pydantic schema for structured output. When
                provided the call chains ``.with_structured_output(schema)``
                and returns a validated instance of that schema instead of a
                raw ``BaseMessage``.
            tools: Bind exactly these tools for this call (the specialist path).
                ``None`` uses the default binding from ``bind_tools``.
            tool_choice: Force the model's hand — ``"any"`` requires it to call
                one of the bound tools. Used by the specialist that must produce
                a visual, where a model that answers in prose has silently
                failed: the person gets code or a description instead of the
                thing they asked for.
            **model_kwargs: Extra kwargs forwarded to ``LLMRegistry.get`` when
                constructing a one-off model instance (e.g. ``temperature``,
                ``max_tokens``).

        Returns:
            ``BaseMessage`` when ``response_format`` is ``None``, otherwise a
            validated instance of ``response_format``.

        Raises:
            RuntimeError: When all models fail after retries or the total
                timeout budget is exceeded.
        """
        try:
            return await asyncio.wait_for(
                self._call_with_fallback(messages, model_name, response_format, model_kwargs, tools, tool_choice),
                timeout=settings.LLM_TOTAL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.exception(
                "llm_total_timeout_exceeded",
                timeout_seconds=settings.LLM_TOTAL_TIMEOUT,
            )
            raise RuntimeError(f"llm call timed out after {settings.LLM_TOTAL_TIMEOUT}s total budget")

    def get_llm(self) -> Any:
        """Return the current default LLM instance (with default tools bound).

        Returns:
            Current ``BaseChatModel`` instance or ``None`` if not initialised.
        """
        return self._llm

    def bind_tools(self, tools: List) -> "LLMService":
        """Bind the default tool set to the current model.

        Args:
            tools: List of tools to bind.

        Returns:
            Self for method chaining.
        """
        if self._base_llm is not None:
            self._bound_tools = list(tools)
            self._llm = self._base_llm.bind_tools(self._bound_tools) if self._bound_tools else self._base_llm
            logger.debug("tools_bound_to_llm", tool_count=len(self._bound_tools))
        return self

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(settings.MAX_LLM_CALL_RETRIES),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((RateLimitError, APITimeoutError, APIError)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _invoke_with_retry(self, llm: Any, messages: LanguageModelInput) -> Any:
        """Invoke an LLM runnable with automatic per-model retry logic.

        Args:
            llm: Any LangChain ``Runnable`` (plain model or structured-output chain).
            messages: Messages to send.

        Returns:
            The runnable's response (``BaseMessage`` or a ``BaseModel`` instance).

        Raises:
            OpenAIError: Propagated after all retry attempts are exhausted.
        """
        try:
            response = await llm.ainvoke(messages)
            logger.debug("llm_call_successful")
            return response
        except (RateLimitError, APITimeoutError, APIError) as e:
            logger.warning(
                "llm_call_failed_retrying",
                error_type=type(e).__name__,
                error=str(e),
                exc_info=True,
            )
            raise
        except OpenAIError as e:
            logger.error(
                "llm_call_failed",
                error_type=type(e).__name__,
                error=str(e),
            )
            raise

    def _switch_to_next_model(self) -> bool:
        """Advance the default model to the next entry in the registry (circular).

        Mutates ``self._base_llm``, ``self._llm`` and ``self._current_model_index``
        so both the default binding and later per-call bindings use the new model.

        Returns:
            ``True`` on success, ``False`` if the switch failed.
        """
        try:
            next_index = (self._current_model_index + 1) % len(LLMRegistry.LLMS)
            next_entry = LLMRegistry.get_model_at_index(next_index)
            logger.warning(
                "switching_to_next_model",
                from_index=self._current_model_index,
                to_index=next_index,
                to_model=next_entry["name"],
            )
            self._current_model_index = next_index
            self._base_llm = next_entry["llm"]
            self._llm = self._base_llm.bind_tools(self._bound_tools) if self._bound_tools else self._base_llm
            logger.info("model_switched", new_model=next_entry["name"], new_index=next_index)
            return True
        except Exception as e:
            logger.error("model_switch_failed", error=str(e))
            return False

    async def _call_with_fallback(
        self,
        messages: LanguageModelInput,
        model_name: Optional[str],
        response_format: Optional[Type[BaseModel]],
        model_kwargs: dict,
        tools: Optional[Sequence[Any]],
        tool_choice: Optional[str] = None,
    ) -> Union[BaseMessage, BaseModel]:
        """Build path-specific strategies and delegate to the shared fallback loop.

        One-off path (any override set):
            ``get_target`` builds a fresh registry instance each attempt.
            ``advance`` increments a local index — ``self._llm`` is never touched.

        Default / specialist path (no overrides):
            ``get_target`` returns ``self._llm`` (default tools) or the base
            model with ``tools`` bound for this call.
            ``advance`` calls ``_switch_to_next_model`` so bindings persist.
        """

        def _bind(base: Any) -> Any:
            """Bind this call's tools, honouring a forced choice when asked."""
            if not tools:
                return base
            if tool_choice:
                return base.bind_tools(list(tools), tool_choice=tool_choice)
            return base.bind_tools(list(tools))

        def _override_target(idx: int) -> Any:
            base = LLMRegistry.get(LLMRegistry.LLMS[idx]["name"], **model_kwargs)
            if response_format:
                return base.with_structured_output(response_format)
            return _bind(base)

        def _default_target(_: int) -> Any:
            if tools is None:
                return self._llm
            return _bind(self._base_llm)

        def _default_advance(_: int) -> Optional[int]:
            return self._current_model_index if self._switch_to_next_model() else None

        if model_name or response_format or model_kwargs:
            all_names = LLMRegistry.get_all_names()
            if model_name and model_name not in all_names:
                logger.error("requested_model_not_found", model_name=model_name)
                raise ValueError(
                    f"model '{model_name}' not found in registry. available models: {', '.join(all_names)}"
                )

            start = all_names.index(model_name) if model_name else self._current_model_index
            total = len(LLMRegistry.LLMS)
            get_target: Callable[[int], Any] = _override_target

            def _override_advance(idx: int) -> Optional[int]:
                return (idx + 1) % total

            advance: Callable[[int], Optional[int]] = _override_advance
        else:
            start = self._current_model_index
            get_target = _default_target
            advance = _default_advance

        return await self._fallback_loop(messages, start, get_target, advance)

    async def _fallback_loop(
        self,
        messages: LanguageModelInput,
        start: int,
        get_target: Callable[[int], Any],
        advance: Callable[[int], Optional[int]],
    ) -> Any:
        """Shared fallback loop — try each model in turn until one succeeds.

        Args:
            messages: Messages to send.
            start: Registry index to begin from.
            get_target: Returns the ``Runnable`` to invoke for a given index.
            advance: Returns the next index to try, or ``None`` to stop.

        Returns:
            The first successful response.

        Raises:
            RuntimeError: When all models have been exhausted.
        """
        total = len(LLMRegistry.LLMS)
        current = start
        models_tried = 0
        last_error: Optional[Exception] = None

        for models_tried in range(1, total + 1):
            current_name = LLMRegistry.LLMS[current]["name"]
            try:
                return await self._invoke_with_retry(get_target(current), messages)
            except OpenAIError as e:
                last_error = e
                logger.error(
                    "llm_call_failed_after_retries",
                    model=current_name,
                    models_tried=models_tried,
                    total_models=total,
                    error=str(e),
                )
                if models_tried >= total:
                    logger.error(
                        "all_models_failed", models_tried=models_tried, starting_model=LLMRegistry.LLMS[start]["name"]
                    )
                    break
                next_idx = advance(current)
                if next_idx is None:
                    logger.error("failed_to_switch_to_next_model")
                    break
                current = next_idx

        raise RuntimeError(
            f"failed to get response from llm after trying {models_tried} models. last error: {str(last_error)}"
        )


llm_service = LLMService()
