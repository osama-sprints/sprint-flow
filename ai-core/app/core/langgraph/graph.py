"""The LangGraph agent: a rule-based supervisor delegating to tool-scoped specialists.

Shape of the graph (see ``specialists.py`` for the table it is built from)::

    supervisor ──route──> learner_support <──> learner_support_tools
                          back_office     <──> back_office_tools
                          chat            <──> tool_call

``supervisor`` never calls a model. Each specialist node binds exactly its own
tool group for its model call and its executor node runs only that group, so a
learner-facing turn cannot reach an administrative tool whatever the model
emits. A multi-intent message is a ``route_plan``: specialists run in order
and the last one composes the single reply the person sees. ``chat`` and
``tool_call`` are the pre-Sprint-1 node names, kept so paused conversations
from before the supervisor still resume where they stopped.
"""

import asyncio
from typing import (
    Any,
    AsyncGenerator,
    Awaitable,
    Callable,
    Mapping,
    Optional,
    Sequence,
    cast,
)
from urllib.parse import quote_plus

import httpx
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
    convert_to_openai_messages,
)
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools.base import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.errors import (
    GraphBubbleUp,
    GraphInterrupt,
)
from langgraph.graph import (
    END,
    StateGraph,
)
from langgraph.graph.state import (
    Command,
    CompiledStateGraph,
)
from langgraph.types import (
    RetryPolicy,
    StateSnapshot,
)
from pydantic import ValidationError
from psycopg import (
    AsyncConnection,
    sql,
)
from psycopg.rows import (
    DictRow,
    dict_row,
)
from psycopg_pool import AsyncConnectionPool

from app.core.config import settings
from app.core.langgraph.specialists import (
    SPECIALISTS,
    Specialist,
    describe_route,
    specialist_for,
)
from app.core.langgraph.supervisor import supervisor_node
from app.core.langgraph.tools import (
    TOOL_GROUPS,
    tools,
)
from app.core.logging import logger
from app.core.metrics import llm_inference_duration_seconds
from app.core.observability import langfuse_callback_handler
from app.core.prompts import load_system_prompt
from app.schemas import (
    GraphState,
    Message,
)
from app.services.llm import llm_service
from app.services.memory import memory_service
from app.utils import (
    dump_messages,
    extract_text_content,
    prepare_messages,
    process_llm_response,
)
from app.services import (
    attachments,
    executions,
)

PostgresConnPool = AsyncConnectionPool[AsyncConnection[DictRow]]

# Only transient failures are worth retrying. A validation error, an
# authorisation refusal or an integrity violation cannot succeed on a second
# attempt, and retrying them three times on every later message is exactly how
# a conversation got stuck before (see reports/ceremony_bot_failure_report.md).
_TRANSIENT_ERRORS = (httpx.TransportError, ConnectionError, TimeoutError, OSError)


async def _invoke_guarded(tool: BaseTool, tool_call: dict) -> str:
    """Run a tool with model-supplied arguments and never let an exception reach the graph.

    ``guarded_tool`` protects the tool body, but LangChain validates the
    arguments against the tool's schema BEFORE the body runs, so a bad value
    from the model (``ceremony_id="the retro"``) would otherwise escape as an
    exception, fail the node and leave the conversation stuck. Interrupts and
    cancellation are re-raised untouched.

    Args:
        tool: The tool to run.
        tool_call: The model's tool call (``name``, ``args``, ``id``).

    Returns:
        str: The tool result, or a readable ``[VALIDATION_ERROR]`` / ``[SYSTEM_ERROR]``.
    """
    try:
        result = await tool.ainvoke(tool_call["args"])
    except (GraphBubbleUp, asyncio.CancelledError):
        raise
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err.get('loc', ())) or 'argument'}: {err.get('msg', 'invalid')}"
            for err in e.errors()
        )
        logger.warning("tool_call_invalid_arguments", tool=tool_call["name"], detail=problems)
        return f"[VALIDATION_ERROR] The arguments for '{tool_call['name']}' were not valid ({problems}); nothing was done."
    except Exception as e:
        logger.exception("tool_call_crashed", tool=tool_call["name"], error=str(e))
        return (
            f"[SYSTEM_ERROR] Something went wrong while running '{tool_call['name']}', so it was stopped. "
            "Please try again in a moment."
        )
    return result if isinstance(result, str) else str(result)


def interrupt_question(value: Any) -> str:
    """Render an interrupt value for the person.

    Confirming tools interrupt with a structured payload (``question`` plus the
    exact proposal, see ``interrupt_payload``); the person only sees the question.

    Args:
        value: The raw interrupt value.

    Returns:
        str: The text to post.
    """
    if isinstance(value, dict) and "question" in value:
        return str(value["question"])
    return str(value)


def resume_value(reply: str, interrupt: Any) -> Any:
    """Build the value handed back to a paused tool when the person answers.

    A structured interrupt gets its own payload echoed back alongside the reply
    so the tool can verify it is committing exactly what was confirmed; a plain
    ``ask_human`` interrupt gets the plain text it always got.

    Args:
        reply: The person's message.
        interrupt: The raw interrupt value that was pending.

    Returns:
        Any: The resume value.
    """
    if isinstance(interrupt, dict) and "question" in interrupt:
        return {"reply": reply, "interrupt": interrupt}
    return reply


def pending_interrupt(state: StateSnapshot) -> Optional[str]:
    """Return the value of a real, resumable interrupt in a saved state, if any.

    ``state.next`` alone is not proof of an interrupt: a node that raised
    leaves ``next`` populated too. Only a task carrying ``interrupts`` may be
    resumed with ``Command(resume=...)``; anything else must start a fresh turn.

    Args:
        state: The checkpointed state snapshot.

    Returns:
        str | None: The interrupt's question, or None.
    """
    raw = pending_interrupt_value(state)
    return None if raw is None else interrupt_question(raw)


def pending_interrupt_value(state: StateSnapshot) -> Any:
    """Return the raw value of a real, resumable interrupt in a saved state, or None.

    Args:
        state: The checkpointed state snapshot.

    Returns:
        Any: The interrupt value (text or structured payload), or None.
    """
    for task in state.tasks or ():
        interrupts = getattr(task, "interrupts", None) or ()
        if interrupts:
            return interrupts[0].value
    return None


def replies_since_last_human(messages: Sequence[Any]) -> list[AIMessage]:
    """Return the assistant replies produced since the person's last message.

    Used to detect a multi-step continuation: when an earlier specialist has
    already answered part of this turn, its reply sits after the last
    ``HumanMessage`` and the next specialist must fold it into ONE final answer.

    Args:
        messages: The graph's message list.

    Returns:
        list[AIMessage]: Text-bearing assistant messages after the last human message.
    """
    replies: list[AIMessage] = []
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            break
        if isinstance(message, AIMessage) and not message.tool_calls and str(message.content).strip():
            replies.append(message)
    replies.reverse()
    return replies


def route_after_supervisor(state: GraphState) -> str:
    """Conditional edge: the node of the specialist the supervisor chose.

    Args:
        state: The graph state after the supervisor ran.

    Returns:
        str: A specialist node name; ``chat`` for an unknown or missing route.
    """
    return specialist_for(state.route).node_name


class LangGraphAgent:
    """Manages the LangGraph Agent/workflow and interactions with the LLM.

    This class handles the creation and management of the LangGraph workflow,
    including LLM interactions, database connections, and response processing.
    """

    def __init__(
        self,
        llm: Any = None,
        tool_groups: Optional[Mapping[str, Sequence[Any]]] = None,
    ):
        """Initialize the LangGraph Agent with necessary components.

        Args:
            llm: The LLM service to call (``llm_service`` by default). Tests inject a fake.
            tool_groups: Capability groups keyed by ``Specialist.tool_group``
                (``TOOL_GROUPS`` by default). Tests inject recording fakes.
        """
        # Use the LLM service with tools bound
        self.llm_service = llm if llm is not None else llm_service
        self.llm_service.bind_tools(tools)
        self.tool_groups: Mapping[str, Sequence[Any]] = tool_groups if tool_groups is not None else TOOL_GROUPS
        self.tools_by_name = {tool.name: tool for tool in tools}
        self._connection_pool: Optional[PostgresConnPool] = None
        self._graph: Optional[CompiledStateGraph] = None
        logger.info(
            "langgraph_agent_initialized",
            model=settings.DEFAULT_LLM_MODEL,
            environment=settings.ENVIRONMENT.value,
        )

    async def _get_connection_pool(self) -> PostgresConnPool:
        """Get a PostgreSQL connection pool using environment-specific settings.

        Returns:
            AsyncConnectionPool: The open connection pool.

        Raises:
            Exception: If the pool cannot be created, in every environment.
        """
        if self._connection_pool is None:
            try:
                # Configure pool size based on environment
                max_size = settings.POSTGRES_POOL_SIZE

                connection_url = (
                    "postgresql://"
                    f"{quote_plus(settings.POSTGRES_USER)}:{quote_plus(settings.POSTGRES_PASSWORD)}"
                    f"@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}"
                )

                self._connection_pool = AsyncConnectionPool(
                    connection_url,
                    open=False,
                    max_size=max_size,
                    kwargs={
                        "autocommit": True,
                        "connect_timeout": 5,
                        "prepare_threshold": None,
                        "row_factory": dict_row,
                    },
                )
                await self._connection_pool.open()
                logger.info("connection_pool_created", max_size=max_size, environment=settings.ENVIRONMENT.value)
            except Exception as e:
                logger.exception(
                    "connection_pool_creation_failed", error=str(e), environment=settings.ENVIRONMENT.value
                )
                # Never degrade silently: the checkpointer is the only store for
                # conversation history and HITL resume state. Serving without it
                # loses data rather than surfacing an outage.
                raise e
        return self._connection_pool

    def _make_specialist_node(self, spec: Specialist) -> Callable[..., Awaitable[Command]]:
        """Build the model node for one specialist.

        The node binds ONLY the specialist's tool group for its model call, so
        the model cannot see another specialist's tools. After the call it goes
        to the specialist's own executor when tools were requested, to the next
        specialist in ``route_plan`` for a multi-step turn, or ends.

        Args:
            spec: The specialist to build the node for.

        Returns:
            The async node function, named after the specialist's node.
        """

        async def specialist(state: GraphState, config: RunnableConfig) -> Command:
            current_llm = self.llm_service.get_llm()
            model_name = (
                current_llm.model_name
                if current_llm and hasattr(current_llm, "model_name")
                else settings.DEFAULT_LLM_MODEL
            )
            username = config.get("metadata", {}).get("username")
            thread_id = config.get("configurable", {}).get("thread_id")
            prior_replies = replies_since_last_human(state.messages)
            system_prompt = load_system_prompt(
                username=username,
                long_term_memory=state.long_term_memory,
                routing_context=describe_route(spec.route.value, state.route_plan, continuation=bool(prior_replies)),
            )
            messages = prepare_messages(state.messages, system_prompt)
            tool_group = list(self.tool_groups.get(spec.tool_group, ()))

            # A composition specialist must call a tool on its first pass:
            # answering in prose means the person asked for a diagram and got a
            # description of one. Once a tool has run (the previous message is
            # its result) the model is free to write the reply — forcing it
            # again would loop.
            last_message = state.messages[-1] if state.messages else None
            returning_from_tool = isinstance(last_message, ToolMessage)
            tool_choice = "any" if (spec.force_tool_use and tool_group and not returning_from_tool) else None

            # This turn's attachments join the call here, not the checkpoint:
            # the history keeps a one-line summary per file, so images and
            # long extracts are never replayed into every later turn. A turn
            # carrying pictures or scanned pages moves to a model that can see
            # them when the current one cannot.
            llm_messages = attachments.augment_llm_messages(dump_messages(messages))
            vision_model = attachments.vision_model_override(model_name)

            # Progress the person can see, and the point where a cancel lands:
            # the model call is raced against the turn's cancel event.
            await executions.progress(executions.STEP_WRITING if returning_from_tool else executions.STEP_THINKING)

            try:
                with llm_inference_duration_seconds.labels(model=model_name).time():
                    try:
                        response_message = await executions.run_cancellable(
                            self.llm_service.call(
                                llm_messages, model_name=vision_model, tools=tool_group, tool_choice=tool_choice
                            )
                        )
                    except executions.ExecutionCancelled:
                        raise
                    except Exception as forced_error:
                        if tool_choice is None:
                            raise
                        # Not every model behind the proxy accepts a forced tool
                        # choice. Falling back to an unforced call keeps the turn
                        # alive; the prompt still asks for the tool.
                        logger.warning(
                            "forced_tool_choice_rejected",
                            specialist=spec.node_name,
                            error=str(forced_error),
                        )
                        response_message = await executions.run_cancellable(
                            self.llm_service.call(llm_messages, model_name=vision_model, tools=tool_group)
                        )
            except executions.ExecutionCancelled:
                raise
            except Exception as e:
                logger.error(
                    "llm_call_failed_all_models",
                    session_id=thread_id,
                    specialist=spec.node_name,
                    error=str(e),
                    environment=settings.ENVIRONMENT.value,
                )
                raise Exception(f"failed to get llm response after trying all models: {str(e)}")

            response_message = process_llm_response(response_message)
            requested_tools = (
                [call["name"] for call in response_message.tool_calls]
                if isinstance(response_message, AIMessage)
                else []
            )
            has_tool_calls = bool(requested_tools)
            logger.info(
                "llm_response_generated",
                session_id=thread_id,
                model=model_name,
                specialist=spec.node_name,
                route=spec.route.value,
                tool_count=len(tool_group),
                tool_calls=requested_tools,
                continuation=bool(prior_replies),
                remaining_plan=list(state.route_plan),
                environment=settings.ENVIRONMENT.value,
            )

            if has_tool_calls:
                return Command(update={"messages": [response_message]}, goto=spec.tools_node_name)

            if state.route_plan:
                # Multi-step turn: hand over to the next specialist. Its prompt
                # says earlier parts already ran and asks for ONE composed reply.
                next_route = state.route_plan[0]
                next_spec = specialist_for(next_route)
                logger.info(
                    "routing_plan_advanced",
                    session_id=thread_id,
                    from_route=spec.route.value,
                    to_route=next_spec.route.value,
                    remaining_plan=list(state.route_plan[1:]),
                )
                return Command(
                    update={
                        "messages": [response_message],
                        "route": next_spec.route.value,
                        "route_plan": list(state.route_plan[1:]),
                    },
                    goto=next_spec.node_name,
                )

            return Command(update={"messages": [response_message]}, goto=END)

        specialist.__name__ = spec.node_name
        specialist.__qualname__ = f"{type(self).__name__}.{spec.node_name}"
        return specialist

    def _make_tools_node(self, spec: Specialist) -> Callable[..., Awaitable[Command]]:
        """Build the tool-executor node for one specialist.

        It executes ONLY tools in the specialist's group. A name outside the
        group — hallucinated, or belonging to another specialist — gets a
        ``[VALIDATION_ERROR]`` tool message and is never executed. This, not
        the prompt, is what keeps a learner-facing turn away from administrative
        actions.

        Args:
            spec: The specialist to build the node for.

        Returns:
            The async node function, named after the specialist's tools node.
        """

        async def tools_node(state: GraphState) -> Command:
            tool_calls = state.messages[-1].tool_calls
            available = {tool.name: tool for tool in self.tool_groups.get(spec.tool_group, ())}

            async def _execute_tool(tool_call: dict) -> ToolMessage:
                tool = available.get(tool_call["name"])
                if tool is None:
                    # A hallucinated or out-of-group tool name must never execute
                    # anything; tell the model and let it recover.
                    logger.warning(
                        "tool_call_refused_out_of_group",
                        tool=tool_call["name"],
                        route=spec.route.value,
                        tools_node=spec.tools_node_name,
                    )
                    content = (
                        f"[VALIDATION_ERROR] The tool '{tool_call['name']}' is not available here "
                        f"(this part of the conversation is handled by {spec.route.value}); it was not run."
                    )
                else:
                    await executions.progress(executions.step_for_tool(tool_call["name"], tool_call.get("args")))
                    content = await executions.run_cancellable(_invoke_guarded(tool, tool_call))
                return ToolMessage(
                    content=content,
                    name=tool_call["name"],
                    tool_call_id=tool_call["id"],
                )

            # Sequential on purpose: a confirming tool pauses the turn with
            # interrupt(), and the person's answer must resume THAT call. With
            # concurrent execution two confirmations could race for one "yes".
            # On resume LangGraph replays the node in the same order, so the
            # first call gets the answer and any later confirmation asks anew.
            outputs = [await _execute_tool(tool_call) for tool_call in tool_calls]
            return Command(update={"messages": outputs}, goto=spec.node_name)

        tools_node.__name__ = spec.tools_node_name
        tools_node.__qualname__ = f"{type(self).__name__}.{spec.tools_node_name}"
        return tools_node

    def build_graph(self, checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
        """Build and compile the supervisor graph on any checkpointer.

        Shape::

            supervisor --(route)--> learner_support <-> learner_support_tools
                                    back_office     <-> back_office_tools
                                    chat            <-> tool_call

        Each specialist node may also hand over to the next specialist in a
        multi-step plan. ``chat``/``tool_call`` keep their pre-Sprint-1 names so
        checkpoints paused inside ``tool_call`` still resume.

        Args:
            checkpointer: Any LangGraph checkpointer (Postgres in production,
                ``MemorySaver`` in tests and probes).

        Returns:
            CompiledStateGraph: The compiled graph.
        """
        graph_builder = StateGraph(GraphState)
        # Rule-based classifier, no LLM call — see supervisor.py.
        graph_builder.add_node("supervisor", supervisor_node)

        specialist_nodes = tuple(spec.node_name for spec in SPECIALISTS.values())
        for spec in SPECIALISTS.values():
            graph_builder.add_node(
                spec.node_name,
                self._make_specialist_node(spec),
                destinations=(spec.tools_node_name, *specialist_nodes, END),
            )
            graph_builder.add_node(
                spec.tools_node_name,
                self._make_tools_node(spec),
                destinations=(spec.node_name,),
                retry_policy=RetryPolicy(max_attempts=3, retry_on=_TRANSIENT_ERRORS),
            )

        graph_builder.set_entry_point("supervisor")
        graph_builder.add_conditional_edges(
            "supervisor",
            route_after_supervisor,
            {spec.node_name: spec.node_name for spec in SPECIALISTS.values()},
        )
        return graph_builder.compile(
            checkpointer=checkpointer, name=f"{settings.PROJECT_NAME} Agent ({settings.ENVIRONMENT.value})"
        )

    async def create_graph(self) -> CompiledStateGraph:
        """Create and configure the LangGraph workflow.

        Returns:
            CompiledStateGraph: The configured LangGraph instance, always with a checkpointer.

        Raises:
            Exception: If the graph cannot be built, in every environment.
        """
        if self._graph is None:
            try:
                # Raises if the pool cannot be created — no checkpointer, no service.
                connection_pool = await self._get_connection_pool()
                checkpointer = AsyncPostgresSaver(connection_pool)
                await checkpointer.setup()

                self._graph = self.build_graph(checkpointer)

                logger.info(
                    "graph_created",
                    graph_name=f"{settings.PROJECT_NAME} Agent",
                    environment=settings.ENVIRONMENT.value,
                    has_checkpointer=checkpointer is not None,
                    nodes=sorted(self._graph.nodes.keys()),
                )
            except Exception as e:
                logger.exception("graph_creation_failed", error=str(e), environment=settings.ENVIRONMENT.value)
                raise e

        return self._graph

    async def _get_graph(self) -> CompiledStateGraph:
        """Return the compiled graph, creating it on first access.

        Raises:
            Exception: Propagated from ``create_graph()`` when initialisation
                fails. Callers can rely on the return being non-``None``.
        """
        if self._graph is None:
            self._graph = await self.create_graph()
        return self._graph

    @staticmethod
    def _build_config(session_id: str, user_id: Optional[str], username: Optional[str]) -> RunnableConfig:
        callbacks: list[BaseCallbackHandler] = [langfuse_callback_handler] if settings.LANGFUSE_TRACING_ENABLED else []
        return {
            "configurable": {"thread_id": session_id},
            "callbacks": callbacks,
            "metadata": {
                "user_id": user_id,
                "username": username,
                "session_id": session_id,
                "environment": settings.ENVIRONMENT.value,
                "debug": settings.DEBUG,
            },
        }

    async def get_response(
        self,
        messages: list[Message],
        session_id: str,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
    ) -> list[Message]:
        """Get a response from the LLM.

        Args:
            messages (list[Message]): The messages to send to the LLM.
            session_id (str): The session ID for the conversation.
            user_id (Optional[str]): The user ID for the conversation.
            username (Optional[str]): The display name of the user.

        Returns:
            list[Message]: The response from the LLM.
        """
        graph = await self._get_graph()
        config = self._build_config(session_id, user_id, username)

        try:
            # Run state check and memory search concurrently to save 200-500ms
            state, relevant_memory = await asyncio.gather(
                graph.aget_state(config),
                memory_service.search(user_id, messages[-1].content),
            )

            # A pending interrupt is detected from the saved tasks, never from
            # ``state.next``: a second question raised while answering a first
            # one leaves ``next`` empty but the task still carries the interrupt.
            pending = pending_interrupt_value(state)
            if pending is not None:
                # A confirmation question is waiting: this message is the answer.
                # The supervisor is not re-run; the paused node picks up exactly
                # where it left off.
                logger.info("resuming_interrupted_graph", session_id=session_id, next_nodes=state.next)
                response = await graph.ainvoke(
                    Command(resume=resume_value(messages[-1].content, pending)),
                    config=config,
                )
            else:
                if state.next:
                    # A node failed mid-turn and left the checkpoint pointing at
                    # it. That is not an interrupt; treat this as a fresh turn.
                    logger.warning("stale_pending_state_discarded", session_id=session_id, next_nodes=state.next)
                relevant_memory = relevant_memory or "No relevant memory found."
                response = await graph.ainvoke(
                    input={"messages": dump_messages(messages), "long_term_memory": relevant_memory},
                    config=config,
                )

            # Check if the graph was interrupted during this invocation
            state = await graph.aget_state(config)
            interrupt_value = pending_interrupt(state)
            if interrupt_value is not None:
                logger.info("graph_interrupted", session_id=session_id, interrupt_value=interrupt_value)
                return [Message(role="assistant", content=interrupt_value)]

            openai_msgs = cast(list[dict], convert_to_openai_messages(response["messages"]))
            asyncio.create_task(memory_service.add(user_id, openai_msgs, config.get("metadata")))
            return self.__process_messages(response["messages"])
        except GraphInterrupt:
            state = await graph.aget_state(config)
            interrupt_value = pending_interrupt(state) or "Waiting for input."
            logger.info("graph_interrupted", session_id=session_id, interrupt_value=interrupt_value)
            return [Message(role="assistant", content=interrupt_value)]
        except executions.ExecutionCancelled:
            # Asked for, not failed: the conversation layer answers "Stopped".
            raise
        except Exception as e:
            logger.exception("get_response_failed", error=str(e), session_id=session_id)
            raise

    async def get_stream_response(
        self,
        messages: list[Message],
        session_id: str,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """Get a stream response from the LLM.

        Args:
            messages (list[Message]): The messages to send to the LLM.
            session_id (str): The session ID for the conversation.
            user_id (Optional[str]): The user ID for the conversation.
            username (Optional[str]): The display name of the user.

        Yields:
            str: Tokens of the LLM response.
        """
        config = self._build_config(session_id, user_id, username)
        graph = await self._get_graph()

        try:
            # Run state check and memory search concurrently to save 200-500ms
            state, relevant_memory = await asyncio.gather(
                graph.aget_state(config),
                memory_service.search(user_id, messages[-1].content),
            )

            pending = pending_interrupt_value(state)
            if pending is not None:
                logger.info("resuming_interrupted_graph_stream", session_id=session_id, next_nodes=state.next)
                graph_input = Command(resume=resume_value(messages[-1].content, pending))
            else:
                if state.next:
                    logger.warning("stale_pending_state_discarded", session_id=session_id, next_nodes=state.next)
                relevant_memory = relevant_memory or "No relevant memory found."
                graph_input = {"messages": dump_messages(messages), "long_term_memory": relevant_memory}

            async for token, _ in graph.astream(
                graph_input,
                config,
                stream_mode="messages",
            ):
                if not isinstance(token, (AIMessage, AIMessageChunk)):
                    continue

                text = extract_text_content(token.content)
                if text:
                    yield text

            # After streaming completes, check for interrupt or update memory
            state = await graph.aget_state(config)
            interrupt_value = pending_interrupt(state)
            if interrupt_value is not None:
                logger.info("graph_interrupted_stream", session_id=session_id, interrupt_value=interrupt_value)
                yield interrupt_value
            elif state.values and "messages" in state.values:
                openai_msgs = cast(list[dict], convert_to_openai_messages(state.values["messages"]))
                asyncio.create_task(memory_service.add(user_id, openai_msgs, config.get("metadata")))
        except GraphInterrupt:
            state = await graph.aget_state(config)
            interrupt_value = pending_interrupt(state) or "Waiting for input."
            logger.info("graph_interrupted_stream", session_id=session_id, interrupt_value=interrupt_value)
            yield interrupt_value
        except Exception as stream_error:
            logger.exception("stream_processing_failed", error=str(stream_error), session_id=session_id)
            raise stream_error

    async def get_chat_history(self, session_id: str) -> list[Message]:
        """Get the chat history for a given thread ID.

        Args:
            session_id (str): The session ID for the conversation.

        Returns:
            list[Message]: The chat history.
        """
        graph = await self._get_graph()

        config: RunnableConfig = {"configurable": {"thread_id": session_id}}
        state: StateSnapshot = await graph.aget_state(config=config)
        return self.__process_messages(state.values["messages"]) if state.values else []

    def __process_messages(self, messages: list[BaseMessage]) -> list[Message]:
        openai_style_messages = convert_to_openai_messages(messages)
        # keep just assistant and user messages
        return [
            Message(role=message["role"], content=str(message["content"]))
            for message in openai_style_messages
            if message["role"] in ["assistant", "user"] and message["content"]
        ]

    async def clear_chat_history(self, session_id: str) -> None:
        """Clear all chat history for a given thread ID.

        Args:
            session_id: The ID of the session to clear history for.

        Raises:
            Exception: If there's an error clearing the chat history.
        """
        try:
            # Make sure the pool is initialized in the current event loop
            conn_pool = await self._get_connection_pool()
            if conn_pool is None:
                raise RuntimeError("connection pool unavailable; cannot clear chat history")

            # Batch all DELETEs in a single pipeline round-trip
            async with conn_pool.connection() as conn:
                async with conn.pipeline():
                    for table in settings.CHECKPOINT_TABLES:
                        await conn.execute(
                            sql.SQL("DELETE FROM {} WHERE thread_id = %s").format(sql.Identifier(table)),
                            (session_id,),
                        )
                logger.info(
                    "checkpoint_tables_cleared_for_session",
                    tables=settings.CHECKPOINT_TABLES,
                    session_id=session_id,
                )

        except Exception as e:
            logger.error(
                "clear_chat_history_operation_failed",
                session_id=session_id,
                error=str(e),
            )
            raise
