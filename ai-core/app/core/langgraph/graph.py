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
import json
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
from app.core.langgraph.nodes import bind_meeting_tools, policy_retrieval_node

PostgresConnPool = AsyncConnectionPool[AsyncConnection[DictRow]]
_TRANSIENT_ERRORS = (httpx.TransportError, ConnectionError, TimeoutError, OSError)


def _clean_policy_context(docs: list | None) -> str:
    """Return only the pure text content of retrieved documents.
    Strips filename, page numbers and any other metadata so the model
    cannot cite sources.
    """
    if not docs:
        return "No relevant document snippets found."

    cleaned_chunks = []
    for i, doc in enumerate(docs, 1):
        if isinstance(doc, dict):
            text = (
                doc.get("content")
                or doc.get("text")
                or doc.get("page_content")
                or str(doc)
            )
        else:
            text = str(doc)
        text = text.strip()
        if text:
            cleaned_chunks.append(f"[{i}]\n{text}")

    if not cleaned_chunks:
        return "No relevant document snippets found."

    return "\n\n".join(cleaned_chunks)


async def _invoke_guarded(tool: BaseTool, tool_call: dict) -> str:
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
    if isinstance(value, dict) and "question" in value:
        return str(value["question"])
    return str(value)


def resume_value(reply: str, interrupt: Any) -> Any:
    if isinstance(interrupt, dict) and "question" in interrupt:
        return {"reply": reply, "interrupt": interrupt}
    return reply


def pending_interrupt(state: StateSnapshot) -> Optional[str]:
    raw = pending_interrupt_value(state)
    return None if raw is None else interrupt_question(raw)


def pending_interrupt_value(state: StateSnapshot) -> Any:
    for task in state.tasks or ():
        interrupts = getattr(task, "interrupts", None) or ()
        if interrupts:
            return interrupts[0].value
    return None


def replies_since_last_human(messages: Sequence[Any]) -> list[AIMessage]:
    replies: list[AIMessage] = []
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            break
        if isinstance(message, AIMessage) and not message.tool_calls and str(message.content).strip():
            replies.append(message)
    replies.reverse()
    return replies


def route_after_supervisor(state: GraphState) -> str:
    return specialist_for(state.route).node_name


class LangGraphAgent:
    def __init__(
        self,
        llm: Any = None,
        tool_groups: Optional[Mapping[str, Sequence[Any]]] = None,
    ):
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
        if self._connection_pool is None:
            try:
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
                raise e
        return self._connection_pool

    def _make_specialist_node(self, spec: Specialist) -> Callable[..., Awaitable[Command]]:
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

            # CRITICAL: only pure text is given to the model – no filenames or page numbers
            clean_context = _clean_policy_context(state.policy_context)

            system_prompt = load_system_prompt(
                username=username,
                long_term_memory=state.long_term_memory,
                routing_context=describe_route(spec.route.value, state.route_plan, continuation=bool(prior_replies)),
                policy_context=clean_context,
            )
            messages = prepare_messages(state.messages, system_prompt)
            tool_group = list(self.tool_groups.get(spec.tool_group, ()))
            if spec.tool_group == "back_office" and self.tool_groups is TOOL_GROUPS:
                tool_group = bind_meeting_tools(tool_group)
            try:
                with llm_inference_duration_seconds.labels(model=model_name).time():
                    response_message = await self.llm_service.call(dump_messages(messages), tools=tool_group)
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
        async def tools_node(state: GraphState) -> Command:
            tool_calls = state.messages[-1].tool_calls
            tool_group = list(self.tool_groups.get(spec.tool_group, ()))
            if spec.tool_group == "back_office" and self.tool_groups is TOOL_GROUPS:
                tool_group = bind_meeting_tools(tool_group)
            available = {tool.name: tool for tool in tool_group}

            async def _execute_tool(tool_call: dict) -> ToolMessage:
                tool = available.get(tool_call["name"])
                if tool is None:
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
                    content = await _invoke_guarded(tool, tool_call)
                return ToolMessage(
                    content=content,
                    name=tool_call["name"],
                    tool_call_id=tool_call["id"],
                )

            outputs = [await _execute_tool(tool_call) for tool_call in tool_calls]
            return Command(update={"messages": outputs}, goto=spec.node_name)

        tools_node.__name__ = spec.tools_node_name
        tools_node.__qualname__ = f"{type(self).__name__}.{spec.tools_node_name}"
        return tools_node

    def build_graph(self, checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
        graph_builder = StateGraph(GraphState)
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
        graph_builder.add_node("policy_retrieval_node", policy_retrieval_node)
        graph_builder.set_entry_point("supervisor")
        graph_builder.add_conditional_edges(
            "supervisor",
            route_after_supervisor,
            {
                **{spec.node_name: spec.node_name for spec in SPECIALISTS.values()},
                "policy_support": "policy_retrieval_node",
            },
        )
        return graph_builder.compile(
            checkpointer=checkpointer, name=f"{settings.PROJECT_NAME} Agent ({settings.ENVIRONMENT.value})"
        )

    async def create_graph(self) -> CompiledStateGraph:
        if self._graph is None:
            try:
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
                "langfuse_user_id": user_id,
                "langfuse_session_id": session_id,
                "langfuse_tags": [
                    "chat",
                    "production" if settings.ENVIRONMENT.value == "production" else "development",
                ],
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
        thread_history: Optional[list[Message]] = None,
    ) -> list[Message]:
        graph = await self._get_graph()
        config = self._build_config(session_id, user_id, username)
        try:
            state, relevant_memory = await asyncio.gather(
                graph.aget_state(config),
                memory_service.search(user_id, messages[-1].content),
            )
            pending = pending_interrupt_value(state)
            if pending is not None:
                logger.info("resuming_interrupted_graph", session_id=session_id, next_nodes=state.next)
                response = await graph.ainvoke(
                    Command(resume=resume_value(messages[-1].content, pending)),
                    config=config,
                )
            else:
                if state.next:
                    logger.warning("stale_pending_state_discarded", session_id=session_id, next_nodes=state.next)
                relevant_memory = relevant_memory or "No relevant memory found."
                graph_messages = messages
                if thread_history and not (state.values and state.values.get("messages")):
                    graph_messages = [*thread_history, *messages]
                response = await graph.ainvoke(
                    input={"messages": dump_messages(graph_messages), "long_term_memory": relevant_memory},
                    config=config,
                )
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
        config = self._build_config(session_id, user_id, username)
        graph = await self._get_graph()
        try:
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
        graph = await self._get_graph()
        config: RunnableConfig = {"configurable": {"thread_id": session_id}}
        state: StateSnapshot = await graph.aget_state(config=config)
        return self.__process_messages(state.values["messages"]) if state.values else []

    def __process_messages(self, messages: list[BaseMessage]) -> list[Message]:
        openai_style_messages = convert_to_openai_messages(messages)
        return [
            Message(role=message["role"], content=str(message["content"]))
            for message in openai_style_messages
            if message["role"] in ["assistant", "user"] and message["content"]
        ]

    async def clear_chat_history(self, session_id: str) -> None:
        try:
            conn_pool = await self._get_connection_pool()
            if conn_pool is None:
                raise RuntimeError("connection pool unavailable; cannot clear chat history")
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