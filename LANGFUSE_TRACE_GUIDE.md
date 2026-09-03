# Langfuse Trace Examples & Interpretation Guide

Learn what to look for in SprintFlow traces captured by Langfuse.

## Example Trace: Chat Request

### User sends: "Create a new sprint for cohort 2026"

### What you'll see in Langfuse:

```
┌─────────────────────────────────────────────────┐
│ Trace ID: tr_abc123def                          │
│ Duration: 2.34s                                 │
│ User: alice@example.com                         │
│ Session: sess_xyz789                            │
│ Tags: [chat, production]                        │
└─────────────────────────────────────────────────┘
    │
    ├─ Span: supervisor_routing
    │  ├─ Input: "Create a new sprint for cohort 2026"
    │  ├─ Output:
    │  │  {
    │  │    "specialisation": "admin_ops",
    │  │    "sub_intents": ["create_sprint"],
    │  │    "rule": "admin_keyword_list"
    │  │  }
    │  └─ Duration: 0.023s (no LLM call)
    │
    ├─ Span: llm_call
    │  ├─ Model: gemini-3.5-flash
    │  ├─ Input Tokens: 2450
    │  ├─ Output Tokens: 287
    │  ├─ Cost: $0.00234
    │  ├─ Input: (system prompt + user message + context)
    │  ├─ Output: (LLM response)
    │  ├─ Temperature: 0.2
    │  └─ Duration: 1.23s
    │
    ├─ Span: tool_call
    │  ├─ Tool: create_sprint
    │  ├─ Input:
    │  │  {
    │  │    "cohort_id": 2026,
    │  │    "sprint_name": "Sprint 1",
    │  │    "start_date": "2026-09-06",
    │  │    "end_date": "2026-09-20"
    │  │  }
    │  ├─ Output:
    │  │  {
    │  │    "success": true,
    │  │    "sprint_id": 4891,
    │  │    "message": "Sprint created successfully"
    │  │  }
    │  └─ Duration: 0.45s
    │
    ├─ Span: memory_operation
    │  ├─ Operation: add
    │  ├─ Query: "Created sprint 4891 for cohort 2026"
    │  └─ Duration: 0.12s
    │
    └─ Metadata:
       ├─ langfuse_user_id: "alice@example.com"
       ├─ langfuse_session_id: "sess_xyz789"
       ├─ langfuse_tags: ["chat", "production"]
       ├─ environment: "production"
       └─ debug: false
```

## Key Sections Explained

### 1. Trace Metadata
- **Trace ID**: Unique identifier (use this to reference the trace)
- **Duration**: Total time from request to response
- **User**: Who made the request
- **Session**: Groups related messages together
- **Tags**: Categories for filtering

### 2. Supervisor Routing Span
**What it is:** Rule-based classification (no LLM)

**Key fields:**
- `specialisation`: Which agent specialisation (admin_ops, learner_support)
- `sub_intents`: Detected intent keywords
- `rule`: Which routing rule matched

**To look for:** Should always complete in <100ms. If slow, check rule complexity.

### 3. LLM Call Span
**What it is:** The main model request

**Key fields:**
- `model`: Which model was used
- `input_tokens`: Tokens in prompt
- `output_tokens`: Tokens in response
- `cost`: Calculated cost (based on token count)
- `temperature`: Sampling parameter (0.2 = deterministic, 1.0 = creative)

**To look for:**
- Are tokens reasonable? (high = inefficient prompts)
- Is cost increasing? (might need a cheaper model)
- Is latency acceptable? (<2 seconds good for chat)

### 4. Tool Call Span
**What it is:** When the agent uses a tool (create_sprint, assign_role, etc.)

**Key fields:**
- `tool`: Tool name
- `input`: Parameters passed to tool
- `output`: Result or error message
- `status`: SUCCESS or FAILED

**To look for:**
- Did the tool succeed? If not, why?
- Is the output sensible?
- Are there any errors in the response?

### 5. Memory Operation Span
**What it is:** Searching or updating long-term memory

**Key fields:**
- `operation`: "search" or "add"
- `query`: What was searched for / What was stored
- `results`: Memory items retrieved
- `duration`: How long it took

**To look for:**
- Is memory being used? (confirm queries appear)
- Are relevant memories being found?
- Is duration acceptable? (<200ms expected)

## Trace Quality Checklist

### ✅ Good Trace Example

```
Supervisor: ✓ (correctly classified as admin_ops)
LLM Call: ✓ (reasonable tokens, <2s latency)
Tool Call: ✓ (tool succeeded, output matches request)
Memory: ✓ (relevant memory found and stored)
Duration: ✓ (2.3s total is reasonable)
Error Rate: ✓ (no errors in any span)
```

### ❌ Problematic Trace Examples

**Issue: Wrong specialisation**
```
Expected: admin_ops (user is admin)
Got: learner_support
→ Check routing rules, maybe admin status not detected
```

**Issue: Token explosion**
```
Input Tokens: 12,000 (very high)
Cost: $0.45 per request
→ Prompts too verbose, consider summarization
```

**Issue: Tool failure**
```
Tool: create_sprint
Output: "Error: Permission denied"
→ User not authorized, check admin status
```

**Issue: Slow response**
```
Duration: 15+ seconds
LLM latency: 12 seconds
→ Model slow or rate-limited, consider fallback
```

**Issue: Memory not used**
```
Memory Operation: None in trace
→ Long-term memory disabled or not queried
→ Recent messages only, no context from history
```

## Metrics to Track

### Daily Metrics

| Metric | Good | Warning | Bad |
|--------|------|---------|-----|
| Avg Latency | <1.5s | 1.5-3s | >3s |
| Error Rate | <1% | 1-5% | >5% |
| Avg Token Cost | <$0.005 | $0.005-$0.01 | >$0.01 |
| Tool Success Rate | >99% | 95-99% | <95% |
| Memory Found | >80% | 50-80% | <50% |

### Quality Indicators

**Using trace scores (0-1 scale):**

```
1.0: Perfect response, tool worked, memory helpful
0.8: Good response, minor issues
0.6: Acceptable but could be better
0.4: Poor response, some failures
0.0: Failed completely, wrong specialisation
```

After collecting scores:
```
Average Score >0.8 = Healthy system
Average Score 0.6-0.8 = Monitor closely
Average Score <0.6 = Investigate issues
```

## Common Issues & Investigation

### Issue 1: High Error Rate (>5% failures)

**Where to look:**
1. Go to Traces tab
2. Filter: Status = "FAILED" or "ERROR"
3. Drill into a failed trace
4. Check which span failed (supervisor, LLM, tool, memory)

**Example investigation:**
```
Trace with error:
├─ Supervisor: ✓ OK
├─ LLM Call: ✓ OK
├─ Tool (create_sprint): ✗ FAILED
│  └─ Output: "Database connection timeout"
└─ Action: Database is slow, needs optimization
```

### Issue 2: Increasing Costs

**Where to look:**
1. Go to Analytics tab
2. Filter: "LLM Requests" or "Cost"
3. Look at trend over time
4. Check which model is most expensive

**Example investigation:**
```
Daily Cost Trend:
Day 1: $0.50
Day 2: $0.72
Day 3: $1.05
Day 7: $2.34
→ Usage is growing 2x every 3 days
→ Consider enabling sampling
```

### Issue 3: Slow Responses

**Where to look:**
1. Go to Traces tab
2. Filter: Duration > 5 seconds
3. Check which span is slow

**Example investigation:**
```
Slow trace:
├─ Supervisor: 0.02s ✓
├─ LLM Call: 8.34s ✗ (too slow!)
├─ Tool: 0.45s ✓
└─ Memory: 0.10s ✓
→ LLM is slow, check:
   - Model capacity/rate limits
   - Prompt length
   - Network latency
```

## Debugging Using Metadata

### Find traces for specific user:
```
Filter: langfuse_user_id = "alice@example.com"
Result: See all interactions from Alice
```

### Find traces from admin operations:
```
Filter: langfuse_tags contains "admin"
Result: See all admin actions
```

### Find production errors:
```
Filter: 
  - langfuse_tags contains "production"
  - Status = ERROR
Result: Critical errors in production
```

## Setting Up Alerts

### Alert: High Error Rate
```
Condition: Error rate > 5%
Trigger: Daily
Notification: Email
```

### Alert: Response Too Slow
```
Condition: P95 Latency > 3 seconds
Trigger: Hourly
Notification: Slack
```

### Alert: Unexpected Cost Increase
```
Condition: Daily cost > $10
Trigger: Daily
Notification: Email
```

## Example Queries

### Query: "Get average cost per user"
```
Filter: Time range = last 7 days
Group by: langfuse_user_id
Metric: Average(cost)
Result: Top expensive users
```

### Query: "Which tools are failing most?"
```
Filter: Status = ERROR
Group by: Tool name
Result: Failure rate by tool
```

### Query: "Response quality by specialisation"
```
Filter: All traces with scores
Group by: specialisation
Metric: Average(score)
Result: Which agent works best
```

## Next Steps

1. **Set up dashboard** with your key metrics
2. **Review traces daily** for patterns
3. **Score low-confidence traces** to track quality
4. **Create alerts** for your thresholds
5. **Analyze trends** weekly to identify improvements

---

**Want more help?** See [langfuse-integration.md](./ai-core/docs/langfuse-integration.md) for advanced topics.
