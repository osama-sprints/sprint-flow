# Langfuse Tracing Integration — Best Practices

This document describes the Langfuse integration in SprintFlow and how to use it effectively for observability and debugging.

## Overview

Langfuse is an open-source LLM observability platform that provides:
- **Application Tracing**: Complete record of every LLM interaction
- **Cost Tracking**: Automatic token usage and cost attribution
- **Performance Monitoring**: Latency, throughput, and quality metrics
- **Prompt Management**: Version control and A/B testing for prompts
- **Evaluation & Scoring**: Custom metrics and automated quality checks
- **User Analytics**: Track usage patterns per user, session, or cohort

## Configuration

### Prerequisites

1. **Langfuse Account**: Create a free account at https://cloud.langfuse.com or self-host
2. **API Keys**: Get your public and secret keys from your Langfuse project settings
3. **Environment**: Update your `.env` file with credentials

### Environment Setup

```bash
# Enable tracing
LANGFUSE_TRACING_ENABLED=true

# Langfuse Cloud (https://us.cloud.langfuse.com for US region)
LANGFUSE_PUBLIC_KEY="pk-lf-your-public-key"
LANGFUSE_SECRET_KEY="sk-lf-your-secret-key"
LANGFUSE_HOST="https://us.cloud.langfuse.com"

# Optional: Debug mode (verbose logging)
LANGFUSE_DEBUG=false

# Optional: Sample rate (1.0 = 100%, reduce for high-volume apps)
LANGFUSE_SAMPLE_RATE=1.0
```

### Region Selection

- **EU** (default): `https://cloud.langfuse.com`
- **US**: `https://us.cloud.langfuse.com`
- **Japan**: `https://jp.cloud.langfuse.com`
- **HIPAA**: `https://hipaa.cloud.langfuse.com`
- **Self-hosted**: `https://your-domain.com`

## Architecture

### How Tracing Works

```
User Request
    ↓
FastAPI API Route
    ↓
LangGraph Agent
    ├─ Supervisor Node (routing)
    ├─ Chat Node (LLM call)
    │   └─ Langfuse Callback Handler [TRACED]
    ├─ Tool Call Node
    │   └─ Tool execution [TRACED]
    └─ Memory Operations [TRACED]
    ↓
Langfuse Callback Handler
    ↓
Batched Async Queue
    ↓
Langfuse API
    ↓
Dashboard & Analytics
```

### Trace Structure

Each user message produces a **Trace** containing:

```
Trace (session_id, user_id, tags)
├─ Span: supervisor_routing
│   ├─ Output: specialisation, intent detection
│   └─ Metadata: routing rules applied
├─ Span: llm_call
│   ├─ Input: system prompt, messages, context
│   ├─ Output: LLM response
│   ├─ Model: gpt-4o, gemini-3.5-flash, etc.
│   ├─ Tokens: input_tokens, output_tokens, cost
│   └─ Duration: 1.23s
├─ Span: tool_execution (if tools are called)
│   ├─ Tool: create_sprint, assign_role, etc.
│   ├─ Input: tool parameters
│   ├─ Output: tool result
│   └─ Duration: 0.45s
└─ Span: memory_operation
    ├─ Operation: search, add
    ├─ Query/Data: semantic search or memory update
    └─ Duration: 0.12s
```

## Key Features Used

### 1. User & Session Tracking

Every trace is automatically attributed to a user and session:

```python
# In graph.py - Automatic attribution via metadata
config = {
    "metadata": {
        "langfuse_user_id": user_id,        # Track per user
        "langfuse_session_id": session_id,  # Group related messages
        "langfuse_tags": ["chat", "production"],
    }
}
```

**In Langfuse Dashboard**:
- Navigate to a user to see all their interactions
- Group messages by session to see conversation flow
- Filter by tags (chat, admin, production, etc.)

### 2. Automatic LLM Tracing

The Langfuse `CallbackHandler` automatically captures:
- LLM model name and version
- Input/output tokens
- Cost calculation
- Latency
- Temperature, max_tokens, and other parameters
- Error messages and retries

**Example trace in Langfuse**:
```
LLM Call: gemini-3.5-flash
├─ Input Tokens: 245
├─ Output Tokens: 87
├─ Cost: $0.00234
├─ Duration: 1.23 seconds
└─ Temperature: 0.2
```

### 3. Tool Call Tracking

When the agent uses tools (create_sprint, assign_role, etc.):

```
Tool Call: create_sprint
├─ Input: {"cohort_id": 2026, "name": "Sprint 1"}
├─ Status: SUCCESS | FAILED
├─ Duration: 0.45s
└─ Output: {"sprint_id": 123}
```

### 4. Memory Operations

Long-term memory searches and updates are traced:

```
Memory Search
├─ Query: "What sprints are active?"
├─ Results: 3 memories found
└─ Duration: 0.12s
```

## Best Practices

### 1. Environment-Specific Configuration

```python
# In production, use lower sample rate to reduce costs
if settings.ENVIRONMENT == "production":
    LANGFUSE_SAMPLE_RATE=0.1  # 10% sampling
else:
    LANGFUSE_SAMPLE_RATE=1.0  # 100% in development
```

### 2. Attribute Traces for Better Debugging

The `langfuse_` prefixed metadata keys are automatically used by Langfuse:

```python
# Good: Langfuse will extract these automatically
config["metadata"] = {
    "langfuse_user_id": user_id,
    "langfuse_session_id": session_id,
    "langfuse_tags": ["cohort:2026", "admin:true"],
}

# Avoid: Don't repeat - use langfuse_ prefix
config["metadata"] = {
    "user_id": user_id,
    "session_id": session_id,
}
```

### 3. Monitoring Key Metrics

Set up Langfuse dashboards to monitor:

**Cost Tracking**:
- Daily LLM spend by model
- Cost per user/cohort
- Expensive operations

**Performance**:
- Average response latency
- P95/P99 latencies
- Tool execution times

**Quality**:
- Error rates
- Tool success/failure ratio
- Retry frequencies

### 4. Scoring & Evaluation

Evaluate trace quality after collection:

```python
# Via Langfuse UI or API
langfuse.score_trace(
    trace_id="tr_123456",
    name="user_satisfaction",
    value=0.8,  # 0.0 to 1.0
    data_type="NUMERIC",
    comment="User confirmed this was helpful"
)
```

### 5. Troubleshooting

**Traces not appearing**:
1. Verify API keys are correct: `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`
2. Check `LANGFUSE_HOST` matches your region
3. Ensure `LANGFUSE_TRACING_ENABLED=true`
4. Look at logs: `LOG_LEVEL=DEBUG` for verbose output

**High latency from Langfuse**:
1. Langfuse operates asynchronously — it shouldn't affect response time
2. If it does, check network connectivity to Langfuse
3. Try enabling `LANGFUSE_DEBUG=true` to see batch operations

**Cost concerns**:
1. Use sampling: `LANGFUSE_SAMPLE_RATE=0.1` (10% of requests)
2. Reduce trace verbosity (disable less-critical spans)
3. Archive old traces in Langfuse

## Integration Points

### 1. Application Startup

```python
# main.py - Initialization
from app.core.observability import langfuse_init
langfuse_init()
```

### 2. Application Shutdown

```python
# main.py - Graceful shutdown
from app.core.observability import shutdown_langfuse
shutdown_langfuse()  # Flushes pending traces
```

### 3. LangGraph Invocation

```python
# graph.py - Automatic callback injection
callbacks = [langfuse_callback_handler] if settings.LANGFUSE_TRACING_ENABLED else []
result = await graph.ainvoke(input, config={"callbacks": callbacks, "metadata": {...}})
```

### 4. Custom Tracing (Optional)

For complex flows, add custom spans:

```python
from langfuse import get_client

langfuse = get_client()

with langfuse.start_as_current_observation(as_type="span", name="data_processing") as span:
    # Your custom logic here
    span.update(input={"raw_data": data})
    result = process_data(data)
    span.update(output={"processed": result})
```

## Viewing Traces

### In Langfuse Dashboard

1. **Navigate to Traces**: https://cloud.langfuse.com/project/YOUR_PROJECT/traces
2. **Filter by**:
   - User: See all interactions from a specific user
   - Session: Group messages in a conversation
   - Tags: Filter by deployment (production, staging, etc.)
   - Time: Historical analysis
3. **Drill Down**: Click any trace to see:
   - Detailed LLM prompts and responses
   - Token usage and cost
   - Tool calls and results
   - Error messages and stack traces
4. **Compare**: A/B test different models or prompts side-by-side

### Via CLI

```bash
# List recent traces
curl -s "https://cloud.langfuse.com/api/v2/projects/YOUR_PROJECT/traces" \
  -H "Authorization: Bearer sk-lf-YOUR_SECRET_KEY"

# Get specific trace
curl -s "https://cloud.langfuse.com/api/v2/traces/TRACE_ID" \
  -H "Authorization: Bearer sk-lf-YOUR_SECRET_KEY"
```

### Programmatically

```python
from langfuse import get_client

langfuse = get_client()

# List traces
traces = langfuse.get_traces(limit=10)
for trace in traces.data:
    print(f"{trace.id}: {trace.name} ({trace.user_id})")

# Get specific trace
trace = langfuse.get_trace(trace_id="tr_123")
print(trace.observations)  # See all nested operations
```

## Cost Estimation

Langfuse is free up to **5M traces/month**. Pricing:

- Development: Usually free tier is sufficient
- Production: ~$0.01-$0.10 per 1000 traces depending on sampling
- Self-hosted: Unlimited (run your own instance)

To estimate:
- 100 users × 10 messages/day = 1,000 traces/day = 30k/month (free tier)
- 1,000 users × 10 messages/day = 10,000 traces/day = 300k/month (still free tier)

## Advanced Usage

### Experiment Tracking

Test different prompts or models:

```python
langfuse.update_trace(
    trace_id=trace_id,
    metadata={"experiment": "gpt-4o-vs-gemini"}
)
```

### Prompt Management

Version your prompts in Langfuse:

```python
from langfuse import get_client

langfuse = get_client()
prompt = langfuse.get_prompt("system-prompt", version=1)
```

### Evaluation Datasets

Create datasets for automated testing:

```python
# Create dataset
dataset = langfuse.create_dataset(name="user_queries")
dataset.items.create(input={"query": "Who am I?"})

# Run experiments against dataset
for item in dataset.items:
    result = agent.invoke(item.input)
    # Compare against expected output
```

## Related Documentation

- [Langfuse Docs](https://langfuse.com/docs)
- [LangChain Integration](https://langfuse.com/docs/integrations/langchain)
- [Tracing Best Practices](https://langfuse.com/docs/observability/best-practices)
- [Evaluation & Scoring](https://langfuse.com/docs/evaluation)
- [Python SDK Reference](https://python.sdk.langfuse.com)
