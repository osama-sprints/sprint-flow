# Langfuse Quick Start Guide

Get started with Langfuse tracing in SprintFlow in 5 minutes.

## Step 1: Create Langfuse Account (2 min)

1. Go to https://cloud.langfuse.com
2. Sign up with email or OAuth
3. Create a new project (or use "default")
4. Navigate to **Settings → API Keys**
5. Copy your keys:
   - `LANGFUSE_PUBLIC_KEY` (starts with `pk-lf-`)
   - `LANGFUSE_SECRET_KEY` (starts with `sk-lf-`)

## Step 2: Update Configuration (1 min)

Update your `.env` file:

```bash
# .env
LANGFUSE_TRACING_ENABLED=true
LANGFUSE_PUBLIC_KEY="pk-lf-your-key-here"
LANGFUSE_SECRET_KEY="sk-lf-your-key-here"
LANGFUSE_HOST="https://us.cloud.langfuse.com"  # US region
# OR https://cloud.langfuse.com for EU
```

## Step 3: Restart Application (1 min)

```bash
# Stop the running application
make down

# Start it again
make up
```

Check logs to see Langfuse initialization:
```bash
docker compose logs ai-core | grep langfuse
# Should see: "langfuse_initialized"
```

## Step 4: Send Test Message (1 min)

Use the Mattermost UI or API to send a message to the bot:

```bash
# Via Mattermost UI
1. Open http://localhost:8065
2. Send a message like "Tell me about AI"
3. The bot responds
```

## Step 5: View Trace in Dashboard (0 min)

1. Go to https://cloud.langfuse.com/project/YOUR_PROJECT/traces
2. You should see traces appearing in real-time! 🎉

Look for:
- **LLM Call**: Your message sent to the model
- **Tool Calls**: Any tools the agent used
- **Output**: The bot's response
- **Cost**: Tokens and cost automatically calculated

## Key Metrics to Monitor

### Dashboard Views

**Traces Table**
- Filter by user, session, tag, time
- Click to drill down into details
- See input/output prompts
- View token usage and cost

**Analytics**
- **Daily cost**: LLM spend over time
- **Model usage**: Which models are being used
- **Error rates**: Failed requests
- **Latency**: Average response time

**Users**
- See all interactions per user
- Track usage patterns
- Find power users

## Common Tasks

### Filter Traces by User
1. Go to Traces tab
2. Click **Filters**
3. Select **User** and enter username
4. See all messages from that user

### Group Messages by Session
1. Go to **Sessions** tab (or use trace list)
2. Filter by `session_id`
3. See full conversation history

### Check LLM Costs
1. Go to **Analytics → LLM Requests**
2. See token usage per model
3. Check daily spend trends

### Score a Trace
1. Open a trace
2. Click **Score** button
3. Give it a quality rating (0-1)
4. Add comment (optional)
5. Use scores to track improvement

## Sampling for Cost Reduction

For high-volume applications (1000+ users):

```env
# Reduce sampling to lower costs
LANGFUSE_SAMPLE_RATE=0.1  # Only trace 10% of requests

# This still captures errors (100% of errors are traced)
# And provides representative data for analytics
```

## Debugging Issues

### Traces not appearing?

Check logs:
```bash
# View Langfuse initialization
docker compose logs ai-core | grep langfuse

# Should show "langfuse_initialized" not "langfuse_tracing_disabled"
```

Verify configuration:
```bash
# Check .env file exists and has correct values
cat .env | grep LANGFUSE

# Restart container to reload .env
docker compose restart ai-core
```

### Wrong region causing issues?

```bash
# Test connectivity to Langfuse
curl -I "https://us.cloud.langfuse.com/"  # Should be 200
# OR
curl -I "https://cloud.langfuse.com/"     # For EU
```

### API keys wrong?

1. Go back to Langfuse dashboard
2. Verify keys in **Settings → API Keys**
3. Test authentication in dashboard
4. Update `.env` and restart

## Pro Tips

💡 **Use tags to organize traces**
```python
# Automatically added by SprintFlow
"langfuse_tags": ["chat", "production"]  # Filter by these
```

💡 **Monitor specific cohorts**
```python
# Include cohort info in tags
"langfuse_tags": ["cohort:2026", "sprint:1"]
```

💡 **Set up alerts**
1. Go to **Alerts** in Langfuse dashboard
2. Create alert for high error rate
3. Get notified via email or Slack

💡 **Export traces for analysis**
```bash
# Use Langfuse CLI
npx langfuse-cli export --project YOUR_PROJECT
```

## Next Steps

1. ✅ Verify traces appear in dashboard
2. ✅ Set up custom dashboard for key metrics
3. ✅ Start labeling traces as good/bad
4. ✅ Monitor costs and adjust sampling if needed
5. ✅ Use traces to identify improvement opportunities

## Help & Support

- **Langfuse Docs**: https://langfuse.com/docs
- **Chat Support**: https://cloud.langfuse.com (in-app chat)
- **GitHub Issues**: https://github.com/langfuse/langfuse/issues
- **SprintFlow Docs**: See [langfuse-integration.md](./ai-core/docs/langfuse-integration.md)

---

**All set!** Your LLM interactions are now being traced. 🚀
