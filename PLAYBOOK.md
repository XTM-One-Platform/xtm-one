# XTM One – Vibe Coding Playbook

A practical, copy-paste guide for adding new things to the platform. No fluff — just patterns that work.

---

## Add a New Agent Template (5 min)

The fastest way to add a new use case. No code changes needed — just data.

### Option A: Via the API (recommended for testing)

```bash
curl -X POST http://localhost:8100/api/v1/agents/templates \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "slug": "meeting-prep",
    "name": "Meeting Prep",
    "description": "Prepare briefings before meetings using calendar + knowledge bases",
    "icon": "Calendar",
    "category": "productivity",
    "required_integrations": ["microsoft"],
    "recommended_mcp": ["notion"],
    "default_persona": "You help prepare meeting briefings. For each upcoming meeting, gather context about attendees, review recent emails/messages with them, and prepare talking points.",
    "default_assignments": [
      {
        "name": "Daily meeting prep",
        "trigger_type": "schedule",
        "trigger_config": {"cron": "0 7 * * 1-5"},
        "prompt": "Check my calendar for today. For each meeting:\n1. Who are the attendees?\n2. What recent emails/messages have I exchanged with them?\n3. What is the likely agenda?\n4. Prepare 3-5 talking points.\n\nDeliver as a morning briefing.",
        "notify_on_complete": true
      }
    ]
  }'
```

### Option B: Via seed data (for built-in templates)

Add to `backend/app/agents/templates.py` → `BUILTIN_TEMPLATES` list:

```python
{
    "slug": "meeting-prep",
    "name": "Meeting Prep",
    "description": "Prepare briefings before meetings",
    "icon": "Calendar",          # Any Lucide icon name
    "category": "productivity",  # communication | development | productivity | automation
    "required_integrations": ["microsoft"],
    "recommended_mcp": ["notion"],
    "default_persona": "You help prepare meeting briefings...",
    "default_assignments": [
        {
            "name": "Daily meeting prep",
            "trigger_type": "schedule",         # schedule | webhook | integration_event | manual
            "trigger_config": {"cron": "0 7 * * 1-5"},
            "activation_prompt": None,          # Optional YES/NO gate
            "prompt": "Check my calendar for today...",
            "delay": None,                      # Optional: "10m", "1h"
            "ai_label": False,
            "read_knowledge_base_ids": None,   # None/[] = all agent KBs; subset = refine
            "write_knowledge_base_ids": None,  # None = all agent-writable KBs; [] = none
            "notify_on_complete": True,
        },
    ],
},
```

Restart the backend — built-in templates are fully synced on every startup (new templates are inserted, existing built-in templates are updated with latest content, and removed built-in slugs are deleted).

### Option C: Via the Settings UI (admin)

Settings → Agent Templates → currently shows existing templates. You can edit name, description, persona, enable/disable. For creating new templates via UI, use the API for now.

---

## Add a New Assignment to an Existing Agent (2 min)

### Via the UI

1. Go to **Agents** page
2. Expand the agent card
3. Click **"+ Add Assignment"**
4. Fill in:
   - **Name**: descriptive name
   - **Trigger type**: manual / schedule / webhook / event
   - **Trigger config**: cron expression, webhook path, or event type + channels
   - **Activation prompt** (optional): "Should I run? YES/NO" — saves cost
   - **Action prompt**: what the AI should do
   - **Tools & Knowledge**: select MCP servers and toggle KB access
   - **Delay**: optional wait (e.g. "10m")
### Via the API

```bash
curl -X POST http://localhost:8100/api/v1/assignments \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "AGENT_UUID",
    "name": "Weekly customer check-in",
    "trigger_type": "schedule",
    "trigger_config": {"cron": "0 9 * * 1"},
    "prompt": "Review all customer interactions from last week. Flag any at-risk accounts. Draft follow-up emails for top 5 accounts.",
    "read_knowledge_base_ids": null
  }'
```

---

## Assignment Trigger Patterns

### Cron schedule
```json
{"trigger_type": "schedule", "trigger_config": {"cron": "0 8 * * 1-5"}}
```
Common patterns: `*/10 * * * *` (every 10min), `0 8 * * 1-5` (8am weekdays), `0 9 * * 1` (Monday 9am)

### Webhook (external HTTP POST)
```json
{"trigger_type": "webhook", "trigger_config": {"path": "my-hook", "webhook_secret": "optional-hmac-secret", "events": ["issues.closed"]}}
```
URL: `POST /api/hooks/{agent_id}/my-hook`
Supports GitHub, Slack, and generic HMAC-SHA256 signature validation.

### Integration event
```json
{"trigger_type": "integration_event", "trigger_config": {"provider": "microsoft", "event": "new_email", "filter": {"channels": ["support"]}}}
```

### Manual
```json
{"trigger_type": "manual", "trigger_config": {}}
```
Trigger via API: `POST /api/v1/assignments/{id}/trigger`

---

## Add a New Integration Provider (10 min)

### Step 1: Provider class (optional, for validation)

```python
# backend/app/integrations/providers/notion.py
import httpx
from app.integrations.base import BaseIntegration, OAuthDefaults

class NotionIntegration(BaseIntegration):
    provider_name = "notion"
    display_name = "Notion"
    icon = "BookOpen"
    description = "Connect Notion for wikis, docs, and meeting notes."
    auth_types = ["oauth", "api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults:
        return OAuthDefaults(
            authorize_url="https://api.notion.com/v1/oauth/authorize",
            token_url="https://api.notion.com/v1/oauth/token",
            scopes=[],
        )

    async def validate_connection(self, token: str) -> bool:
        async with httpx.AsyncClient() as client:
            resp = await client.get("https://api.notion.com/v1/users/me",
                headers={"Authorization": f"Bearer {token}", "Notion-Version": "2025-09-03"})
            return resp.status_code == 200

    async def get_user_info(self, token: str) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.get("https://api.notion.com/v1/users/me",
                headers={"Authorization": f"Bearer {token}", "Notion-Version": "2025-09-03"})
            resp.raise_for_status()
            return resp.json()
```

### Step 2: Add to provider catalog

In `backend/app/api/v1/integration_catalog.py` → `PROVIDER_CATALOG`:

```python
"notion": {
    "name": "Notion",
    "auth_type": "oauth",
    "fields": ["client_id", "client_secret"],
    "scopes": "",
    "authorize_url": "https://api.notion.com/v1/oauth/authorize",
    "token_url": "https://api.notion.com/v1/oauth/token",
},
```

### Step 3: Create `build_tools()` function

In the same provider file, add a `build_tools()` function that returns tool definitions:

```python
# backend/app/integrations/providers/notion.py (continued)
from app.tools.base import CallableToolExecutor, ToolDefinition

def build_tools(token: str, executor: CallableToolExecutor, **_kwargs) -> list[ToolDefinition]:
    headers = {"Authorization": f"Bearer {token}", "Notion-Version": "2025-09-03"}

    async def search_notion(query: str = "") -> str:
        # ... implementation ...
        pass

    executor.register("search_notion", search_notion)
    return [
        ToolDefinition(
            name="search_notion",
            description="Search Notion pages and databases.",
            input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            source="integration:notion",
        ),
    ]
```

### Step 4: Register in `PROVIDER_TOOLS`

In `backend/app/services/integration_tools.py`, import and register the builder:

```python
from app.integrations.providers.notion import build_tools as notion_tools

PROVIDER_TOOLS = {
    ...,
    "notion": notion_tools,
}
```

That's it. Users create Notion integrations in the UI, paste their OAuth credentials, and authorize.

---

## Add an MCP Tool Server (2 min, no code)

### Via the Settings UI

Settings → MCP Tool Servers → Add:
- **Name**: "GitHub"
- **Transport**: stdio
- **Command**: `npx`
- **Args**: `["-y", "@modelcontextprotocol/server-github"]`
- **Env vars**: `{"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_..."}`

### Common MCP servers

| Server | Command | Env |
|--------|---------|-----|
| GitHub | `npx -y @modelcontextprotocol/server-github` | `GITHUB_PERSONAL_ACCESS_TOKEN` |
| Notion | `npx -y @notionhq/notion-mcp-server` | `OPENAPI_MCP_HEADERS={"Authorization":"Bearer ntn_...","Notion-Version":"2025-09-03"}` |
| Slack | `npx -y @modelcontextprotocol/server-slack` | `SLACK_BOT_TOKEN` |
| Filesystem | `npx -y @modelcontextprotocol/server-filesystem /path` | — |
| Brave Search | `npx -y @modelcontextprotocol/server-brave-search` | `BRAVE_API_KEY` |

Browse more: https://github.com/modelcontextprotocol/servers

---

## Add a Knowledge Base + Documents (3 min, no code)

### Via the UI

1. **Knowledge** page → **New Knowledge Base**
2. Name it (e.g. "Product Documentation", "MEDDPICC Framework")
3. Add documents:
   - **Drag & drop** files (.txt, .md, .csv, .json, .pdf, .docx, .xls, .xlsx, .pptx)
   - **Upload** button for file picker
   - **Paste text** button for copy-paste
5. Go to **Agents** → edit your agent → toggle the KB on in **Knowledge Bases** section

The agent's `search_knowledge` tool will now find content from this KB during chat and assignments.

---

## Agent Design Patterns

### Pattern: "Triage then draft"
Two assignments chained: first triages/flags, second drafts responses for flagged items.
```
Assignment 1: Triage → flags important items
Assignment 2: Draft → processes flagged items (schedule: every 10min)
```

### Pattern: "Wait then check"
Webhook trigger + delay: wait N minutes, then verify a condition.
```json
{"trigger_type": "webhook", "delay": "10m", "prompt": "Check if the label was added..."}
```

### Pattern: "Learn then apply"
Nightly learning assignment updates a KB, daily action assignment uses it.
```
Assignment 1: Learn (2am) → analyzes sent emails, updates style KB
Assignment 2: Draft (every 10min) → uses style KB for tone matching
```

### Pattern: "Activation gate"
Cheap YES/NO check before expensive main prompt:
```json
{
  "activation_prompt": "Is this a question in my monitored channels? YES or NO.",
  "prompt": "Search knowledge bases and answer the question..."
}
```

### Pattern: "Multi-channel digest"
One agent, many assignments — one per channel with its own cron:
```
Agent: "My Digests"
├── Assignment: "Leadership daily"    (cron: 0 8 * * 1-5)
├── Assignment: "Engineering weekly"  (cron: 0 9 * * 1)
└── Assignment: "Sales monthly"      (cron: 0 9 1 * *)
```

### Pattern: "Webhook → MCP action"
External system sends data → AI analyzes → MCP tool takes action:
```json
{
  "trigger_type": "webhook",
  "trigger_config": {"path": "opencti-reports"},
  "prompt": "Analyze this report. If it's relevant to our company, use the OpenCTI MCP to add the label 'filigran-relevant'."
}
```

### Pattern: "Sub-agent delegation"
A coordinator agent delegates specialized work to peer agents asynchronously:
```
Agent: "Project Coordinator"
  ├── handover_agent_ids: [research-agent, writer-agent]
  └── Assignment: "Process incoming requests"
      prompt: "Analyze this request. If it needs research, use spawn_background_task
               to delegate to the research agent. If it needs writing, delegate to
               the writer agent. Check task status periodically with check_task_status."
```
The parent agent continues its conversation while background tasks run. Use `get_task_result(task_id)` to retrieve completed work.

### Pattern: "Memory-augmented responses"
Agents with persistent memory can recall facts across sessions:
```
Agent: "Account Manager"
  ├── memory_short_term_enabled / memory_medium_term_enabled / memory_persistent_enabled: true
  └── Assignment: "Customer check-in"
      prompt: "Before responding, use search_memory to find previous interactions
               and preferences for this customer. Reference specific past conversations
               and action items in your response."
```
The agent's persistent memory accumulates across all conversations and assignments. Facts are auto-extracted during context compaction.

---

## Prompt Engineering Tips

### Use template variables
```
{{trigger.payload}}    — full trigger data (webhook body, event data)
{{trigger.from}}       — sender (for email/message events)
{{trigger.text}}       — message content
{{agent.persona}}      — the agent's persona prompt
{{agent.name}}         — agent display name
{{agent.config}}       — agent's free-form config JSON
```

### Be specific about output format
```
Structure your response as:
## Summary
- ...
## Action Items
- [ ] ...
## Needs Attention
- ...
```

### Tell the AI when NOT to act
```
If this is not a question or not relevant to my channels, do nothing. Do not respond.
```

### Reference knowledge bases explicitly
```
Search my knowledge bases for:
1. My writing style guide
2. Previous interactions with this sender
3. Product documentation relevant to the topic
```

---

## Configure LLM Models (2 min)

### Platform default model

Admins set the platform-wide default LLM model in **Settings → General Assistant**. This model is used for:
- General chat assistant (when no agent is selected)
- Agents without a specific model override
- Users without a preferred model in their profile

### Multi-model per provider

Each LLM provider can have multiple available models. In **Settings → LLM Providers**:
1. Add a provider (Anthropic, OpenAI, Gemini, OpenRouter, Ollama, Mistral AI, Azure OpenAI, AWS Bedrock, LiteLLM, Custom OpenAI-Compatible)
2. Select which models to make available (shown as tiles)
3. Click the star to set one as the platform default

**First-time setup**: When you configure your first provider and select models, the first model automatically becomes the platform default.

### Agent-level model override

In an agent's **Configuration** tab, select a specific model. This overrides both the user's preference and the platform default for that agent's assignments and chat.

### User preference

In **Profile**, users can select their preferred LLM model. This overrides the platform default for their general chat conversations but not agent-specific models.

---

## Share Entities with Groups (admin)

Instead of making entities company-managed (visible to all), you can share them with specific user groups:

### Via the UI
1. Open the entity detail page (Agent / Integration / KB / MCP Server / Custom Tool / Channel Bot / Prompt)
2. Go to the **Configuration** tab
3. Find the **Group Sharing** card
4. Toggle ON and select which groups should have access

### Via the API

```bash
curl -X PUT http://localhost:8100/api/v1/agents/{id} \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"shared_group_ids": ["GROUP_UUID_1", "GROUP_UUID_2"]}'
```

### Managing Groups

```bash
# Create a group
curl -X POST http://localhost:8100/api/v1/groups \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "Engineering", "description": "Engineering team"}'

# Add members
curl -X POST http://localhost:8100/api/v1/groups/{group_id}/members \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"user_ids": ["USER_UUID_1", "USER_UUID_2"]}'
```

Entity visibility follows three tiers: **own + group-shared + company-managed**.

---

## Toggle Company-Managed Status (admin)

All seven entity types support a company-managed toggle:

### Via the UI
1. Open the entity detail page (Agent / Integration / KB / MCP Server / Custom Tool / Channel Bot / Prompt)
2. Scroll to the **Company-managed** toggle (admin-only)
3. Toggle on = shared with all users. Toggle off = private to you.

### What happens when toggling

**Private → Company-managed**:
- `user_id` set to NULL, entity visible to all users

**Company-managed → Private**:
- Entity assigned to the admin who toggled it
- References automatically cleaned from other users' agents:
  - Agent: assignments and runs reassigned to admin
  - Integration: removed from other users' agent bindings
  - KB: removed from other users' agent knowledge_base_ids
  - MCP Server: removed from other users' agent mcp_server_ids
  - Custom Tool: removed from other users' agent custom_tool_ids
  - Channel Bot: removed from other users' agent channel configurations

---

## Configure Assignment Prompt Transparency

Assignments have three system prompt injection points that users can customize (defaults are used when NULL):

- **AI label prompt** (`ai_label_prompt`): Appended when `ai_label=true`. Controls the "AI-generated" label behavior.
- **Human-in-the-loop prompt** (`human_in_the_loop_prompt`): Appended when `human_in_the_loop=true`. Controls the approval flow.

### Knowledge base read/write split

Assignments refine the agent's KB access (like tool/integration scoping):

- `read_knowledge_base_ids`: KB IDs the assignment may search. NULL or `[]` = inherit all agent KBs (read has no "none" state); a subset refines to those KBs.
- `write_knowledge_base_ids`: KB IDs the `store_knowledge` tool may write to, refining the agent's writable KBs (those marked `write` in the agent's `knowledge_base_permissions`). NULL = all agent-writable KBs, `[]` = none, subset = refine.

The old `use_knowledge_bases` / `write_knowledge_base_enabled` / `target_knowledge_base_id` fields are deprecated and no longer consulted at execution time.

Edit these in the **Assignment Detail** page under "Prompt Transparency" and "Knowledge Bases" sections.

---

## Configure Channel Routing (per agent)

By default, new agents are only accessible via **Web Chat**. External channels (Slack, Teams, WhatsApp, Telegram, Discord, Google Chat) must be explicitly enabled per agent. This lets you control exactly which agents are exposed on each channel bot — e.g., route internal corporate agents to Teams and community-facing agents to Slack.

### Via the UI

1. Open the agent's detail page
2. Go to the **Configuration** tab
3. Scroll to **Channel Routing**
4. Toggle ON the channels this agent should respond on

### Via the API

```bash
curl -X PUT http://localhost:8100/api/v1/agents/{id} \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"available_channel_types": ["web", "teams"]}'
```

- `["web"]` — web chat only (default for new agents)
- `["web", "teams"]` — web chat + Teams
- `["web", "slack", "teams"]` — web chat + Slack + Teams
- `null` — all channels (legacy behavior, not recommended)

### Best practices

- **Internal agents** (HR, IT support): enable Teams only
- **Community agents** (public support): enable Slack only
- **Customer-facing agents** (WhatsApp support): enable WhatsApp only
- **All agents**: Web Chat is always available unless explicitly disabled

---

## Set Up Channel Bots

Channel bots (Slack, Teams, WhatsApp, Telegram, Discord, Google Chat) are separate from integrations. They use bot tokens, not OAuth, and are managed via **Settings > Channels**.

### Via the UI

1. Go to **Settings > Channels**
2. Click **Add Channel Bot**
3. Select the provider (Slack, Teams, WhatsApp, Telegram, Discord, Google Chat)
4. Follow the setup guide to create credentials and paste them in
5. Configure receive/publish channel whitelists (optional)

### Via the API

```bash
# List available channel bot providers
curl http://localhost:8100/api/v1/channel-bots/catalog \
  -H "Authorization: Bearer $TOKEN"

# Create a Slack bot
curl -X POST http://localhost:8100/api/v1/channel-bots \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"provider": "slack_bot", "name": "My Slack Bot", "api_key": "xoxb-...", "extra_config": {"signing_secret": "..."}}'

# Create a Teams bot
curl -X POST http://localhost:8100/api/v1/channel-bots \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"provider": "teams_bot", "name": "My Teams Bot", "client_id": "...", "client_secret": "...", "extra_config": {"tenant_id": "..."}}'
```

---

## Disable Chat for Assignment-Only Agents

Some agents are designed only for background assignment execution and shouldn't appear in chat. Set `disable_chat: true` on the agent:

- Hidden from `/v1/models` (LLM proxy)
- Hidden from Profile page API keys section
- Shown greyed-out in chat agent picker with a warning

Toggle this in the agent's **Configuration** tab.

---

## Use the LLM Proxy (API keys)

Point any tool to your Copilot instance as an OpenAI-compatible endpoint:

```bash
# Docker Compose — FastAPI serves /v1 directly on the unified platform container
curl http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer fcp-..." \
  -d '{"model":"auto","messages":[{"role":"user","content":"Hello"}]}'

# Dev mode — Vite (port 3100) proxies /v1, or call the backend directly on port 8100
curl http://localhost:3100/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer fcp-..." \
  -d '{"model":"auto","messages":[{"role":"user","content":"Hello"}]}'
```

- `model: "auto"` → uses platform default
- `model: "claude-sonnet-5"` → uses specific model (must be in available models)
- `model: "agent:<slug>"` → routes through an agent (persona + tools + knowledge bases)
- Create API keys in **Profile → API Keys**
- Available endpoints: `GET /v1/models` and `POST /v1/chat/completions`

### Using with Cursor / Continue

Add a custom OpenAI-compatible provider:
- **Base URL**: `http://localhost:4000/v1` (Docker) or `http://localhost:3100/v1` (dev)
- **API Key**: your `fcp-...` key
- **Model**: `auto`, a specific model ID, or `agent:<slug>`

---

## Add Agent Skills (agentskills.io)

Agent skills are reusable instruction sets that augment agent behavior. Skills can be created from a URL, pasted content, or uploaded file.

### Add a skill (UI)

1. Go to **Skills** → **Add Skill**
2. Choose one of three modes:
   - **URL**: Paste a URL to a SKILL.md file (e.g. `https://raw.githubusercontent.com/org/repo/main/skills/my-skill/SKILL.md`)
   - **Paste**: Paste the skill content directly (provide a name)
   - **Upload**: Upload a SKILL.md file
3. Name, description, and compatibility are auto-detected from YAML frontmatter
4. Attach the skill to agents in the agent's **Configuration** tab via `skill_ids`

### Add a skill (API)

```bash
# From URL:
curl -X POST http://localhost:8100/api/v1/skills \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"url":"https://raw.githubusercontent.com/org/repo/main/skills/my-skill/SKILL.md"}'

# From pasted content:
curl -X POST http://localhost:8100/api/v1/skills \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"content":"---\nname: My Skill\n---\n# Instructions...", "name":"My Skill"}'

# From file upload:
curl -X POST http://localhost:8100/api/v1/skills/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@SKILL.md"
```

### Refresh a skill

URL-based skills cache content from the URL. To re-fetch: `POST /api/v1/skills/{id}/refresh`

---

## Create a Prompt Template (2 min)

Prompts are reusable text blocks that can be attached to agents (injected into system prompts) or used directly in chat conversations.

### Via the UI

1. Go to **Prompts** → **Add Prompt**
2. Enter a name, description, and the prompt content
3. Optionally add tags for filtering
4. Attach to agents in the agent's **Configuration** tab via `prompt_ids`

### Via the API

```bash
# Create a prompt
curl -X POST http://localhost:8100/api/v1/prompts \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Code Review Guidelines",
    "description": "Standards for reviewing code submissions",
    "content": "When reviewing code, focus on:\n1. Security vulnerabilities\n2. Performance implications\n3. Code readability and maintainability\n4. Test coverage\n\nAlways suggest improvements, not just flag issues.",
    "tags": ["development", "review"]
  }'

# Attach prompt to an agent
curl -X PUT http://localhost:8100/api/v1/agents/{id} \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt_ids": ["PROMPT_UUID"]}'
```

Prompts support the same company-managed toggle and group sharing as other entities.

---

## Quick Reference: File Locations

| What | Where |
|------|-------|
| Agent templates (seed data) | `backend/app/agents/templates.py` |
| Assignment model | `backend/app/models/assignment.py` |
| Assignment engine | `backend/app/services/assignment_engine.py` |
| Agent chat (with tools + KB) | `backend/app/services/agent_chat.py` |
| General assistant chat | `backend/app/services/general_chat.py` |
| LLM router (model resolution) | `backend/app/llm/router.py` |
| General assistant config model | `backend/app/models/general_assistant_config.py` |
| Shared API helpers (DRY) | `backend/app/api/v1/helpers.py` |
| Shared backend utils | `backend/app/core/utils.py` |
| Webhook signature verification | `backend/app/core/security.py` (`verify_github_signature`, `verify_slack_signature`) |
| KB search tool factory | `backend/app/services/vector.py` (`make_search_knowledge_fn`) |
| Integration provider catalog | `backend/app/api/v1/integration_catalog.py` → `PROVIDER_CATALOG` |
| Agent API | `backend/app/api/v1/agents.py` |
| Assignment API | `backend/app/api/v1/assignments.py` |
| Webhook receiver | `backend/app/api/v1/webhooks.py` |
| Knowledge bases API | `backend/app/api/v1/knowledge_bases.py` |
| MCP servers API | `backend/app/api/v1/mcp_servers.py` |
| Settings API (LLM, GA, SSO, Langfuse, OAuth defaults, tags, memory purge) | `backend/app/api/v1/settings.py` |
| LLM proxy (OpenAI-compatible) | `backend/app/api/v1/llm_proxy.py` |
| API keys | `backend/app/api/v1/api_keys.py` |
| Custom tools API | `backend/app/api/v1/custom_tools.py` |
| Channel bots API | `backend/app/api/v1/channel_bots.py` |
| Channel bot model | `backend/app/models/channel_bot.py` |
| Custom tool executor | `backend/app/services/custom_tool_executor.py` |
| Code interpreter (Python sandbox) | `backend/app/services/code_interpreter.py` |
| Frontend agent pages | `frontend/src/pages/Agents.tsx`, `AgentDetail.tsx`, `AgentChatConversation.tsx`, `AgentFlow.tsx`, `NewAgent.tsx` |
| Frontend integration pages | `frontend/src/pages/Integrations.tsx`, `IntegrationDetail.tsx`, `NewIntegration.tsx` |
| Frontend KB pages | `frontend/src/pages/KnowledgeBases.tsx`, `KnowledgeBaseDetail.tsx`, `NewKnowledgeBase.tsx` |
| Frontend MCP pages | `frontend/src/pages/MCPServers.tsx`, `MCPServerDetail.tsx` |
| Frontend custom tool pages | `frontend/src/pages/CustomTools.tsx`, `CustomToolDetail.tsx` |
| Frontend settings | `frontend/src/pages/Settings.tsx` |
| Frontend profile | `frontend/src/pages/Profile.tsx` |
| Frontend user management | `frontend/src/pages/Users.tsx`, `UserDetail.tsx`, `NewUser.tsx` |
| Group model | `backend/app/models/group.py` |
| Groups API | `backend/app/api/v1/groups.py` |
| Frontend group pages | `frontend/src/pages/Groups.tsx`, `GroupDetail.tsx`, `NewGroup.tsx` |
| Frontend group hooks | `frontend/src/hooks/api/useGroupApi.ts` |
| Frontend group sharing card | `frontend/src/components/ui/group-sharing-card.tsx` |
| Frontend types | `frontend/src/types/index.ts` |
| Frontend API hooks | `frontend/src/hooks/api/` (one file per domain) |
| Frontend debounce hook | `frontend/src/hooks/useDebounce.ts` |
| Frontend entity list filter hook | `frontend/src/hooks/useEntityFilter.ts` |
| Frontend constants | `frontend/src/lib/constants.ts` |
| Frontend shared utils | `frontend/src/lib/utils.ts` |
| Frontend company-managed badge | `frontend/src/components/ui/company-managed-badge.tsx` |
| Frontend search input | `frontend/src/components/ui/search-input.tsx` |
| Frontend empty state | `frontend/src/components/ui/empty-state.tsx` |
| Frontend graph utilities | `frontend/src/lib/graph-utils.ts` |
| Frontend beam particle | `frontend/src/components/ui/beam-particle.tsx` |
| Frontend assignment run list | `frontend/src/components/ui/assignment-run-list.tsx` |
| Frontend timestamp grid | `frontend/src/components/ui/timestamp-grid.tsx` |
| Frontend not-found state | `frontend/src/components/ui/not-found-state.tsx` |
| Frontend tool scope dialog | `frontend/src/components/ui/tool-scope-dialog.tsx` |
| Sub-agent task service | `backend/app/services/sub_agent.py` |
| Persistent memory service | `backend/app/services/persistent_memory.py` |
| Sub-agent task model | `backend/app/models/sub_agent_task.py` |
| Agent memory entry model | `backend/app/models/agent_memory_entry.py` |
| Telegram channel adapter | `backend/app/channels/telegram.py` |
| Agent skill model | `backend/app/models/agent_skill.py` |
| Agent skills API | `backend/app/api/v1/agent_skills.py` |
| Agent skill schemas | `backend/app/api/v1/schemas/agent_skill.py` |
| Frontend skills pages | `frontend/src/pages/Skills.tsx`, `SkillDetail.tsx`, `skill-detail/` |
| Frontend skill hooks | `frontend/src/hooks/api/useSkillApi.ts` |
| Prompt model | `backend/app/models/prompt.py` |
| Prompts API | `backend/app/api/v1/prompts.py` |
| Prompt schemas | `backend/app/api/v1/schemas/prompt.py` |
| Frontend prompt pages | `frontend/src/pages/Prompts.tsx`, `PromptDetail.tsx` |
| Frontend prompt hooks | `frontend/src/hooks/api/usePromptApi.ts` |
| All dev guides | `**/AGENTS.md` files in each directory |

---

## Validate Alembic Migrations Locally (2 min)

When you add or modify a migration, run these checks before pushing.

### Rules every migration MUST follow (idempotent, single-head, lock-light)

The platform **auto-migrates on boot across multiple replicas**, so one bad migration breaks every booting backend (this keeps recurring). `--autogenerate` output is NOT safe as-is. Before writing one, read `dev-docs/reference/migrations-safety.md`. In short:

- **SINGLE HEAD** - chain off the current head; after a rebase, re-point `down_revision` to main's new head. `test_migration_integrity.py` fails the build on multiple heads.
- **IDEMPOTENT** - guard all DDL: `ADD COLUMN IF NOT EXISTS`, `DROP COLUMN IF EXISTS`, `CREATE TABLE/INDEX IF NOT EXISTS`, `ALTER TYPE ... ADD VALUE IF NOT EXISTS`, `DO $$ ... IF NOT EXISTS ... $$` for constraints. Fresh installs `create_all` + `stamp head` (body never runs) while existing installs run the body - both must converge, and replicas may retry.
- **LOCK-LIGHT** - only `ADD COLUMN ... DEFAULT <constant>` is metadata-only (safe on hot tables). NO table rewrites, NO `SET NOT NULL` on big tables (use `CHECK ... NOT VALID` then `VALIDATE CONSTRAINT`), NO unbounded in-transaction backfills (batch them). NEVER `CREATE/DROP INDEX CONCURRENTLY` (`test_no_new_migration_builds_an_index_concurrently`): under the migration lock it waits on every older transaction in the database and hangs the rollout. Build an index with `create_index_in_place()` from `app.core.migration_ddl` inside `op.get_context().autocommit_block()`, and ask before adding one to a big, hot table.
- **LOCK-SAFE (machine-enforced)** - even a metadata-only `ALTER TABLE` queues behind any long-lived open transaction that has read the table (agentic loops hold sessions for minutes; there is NO cold table - `platform_settings` took production down on 2026-07-05). Every lock-taking DDL must follow the canonical recipe: idempotency probe + early return, then `terminate_lock_holders(conn, "<table>")` + `set_lock_timeout(conn)` from `app.core.migration_ddl`, then the guarded DDL. `test_lock_taking_ddl_sets_lock_timeout` fails the build otherwise (`create_index_in_place()` sets the belt itself). The startup runner retries transient 55P03/57014 lock failures with backoff (a statement timeout once), but the pre-flight is what makes the DDL actually complete.
- **CONCURRENCY** - startup migration is already serialized by a `pg_advisory_lock` in `app/core/migrations.py`. Rely on it; never add a second lock or run DDL outside Alembic.
- **REVERSIBLE - NO EXCEPTIONS** - write a real, working `downgrade()` for every migration. There is no "irreversible migration" concept and no allowlist: if a migration cannot be cleanly downgraded, redesign it. CI fails on any downgrade error.

### 1. Offline integrity check (no database needed)

```bash
cd backend
uv run python -m pytest tests/test_migration_integrity.py -v
```

This parses every migration file with AST and checks:
- No duplicate `revision` IDs
- No orphaned `down_revision` references
- Exactly one head (single linear tip after merges)
- Connected DAG from base(s) to head
- At least one base migration exists

Runs on **every PR** in CI (no path filter — always executes).

### 2. Fresh install test (needs PostgreSQL + pgvector)

```bash
cd backend
export DATABASE_URL="postgresql+asyncpg://copilot:copilot@localhost:5432/copilot"
export SECRET_KEY="dev-secret"
uv run python -m pytest -m integration tests/test_fresh_install.py -v
```

This creates a temporary database, replicates the fresh-install path (`pgvector` → `create_all` → `stamp head`), then verifies the resulting schema matches all SQLAlchemy models (no missing tables or columns).

**Requirements**: PostgreSQL 17 + pgvector running, `DATABASE_URL` user must have `CREATEDB` privilege.

### 3. Manual downgrade round-trip (for new migrations)

```bash
cd backend
# Start from head
uv run python -m alembic -c alembic/alembic.ini upgrade head

# Downgrade your migration
uv run python -m alembic -c alembic/alembic.ini downgrade -1

# Re-upgrade — should succeed cleanly
uv run python -m alembic -c alembic/alembic.ini upgrade head
```

The round-trip must succeed for every migration - there is no "irreversible migration" concept. If your downgrade fails, fix the migration: rebuild the prior shape from the migrated data (a full data-migration downgrade restores dropped tables/columns and re-adds `NOT VALID` FKs), or use a no-op `downgrade()` body when the old state genuinely carried no information (pure normalizations).

### When do DB-backed tests run in CI?

The `migrations` job detects changes to these paths (three-dot diff against the PR base):

| Path | Why |
|------|-----|
| `backend/alembic/` | Migration scripts themselves |
| `backend/app/models/` | Model changes that need a migration |
| `backend/app/core/config.py` | DB config |
| `backend/app/core/migrations.py` | Production migration wrapper |
| `backend/app/core/database.py` | Engine / Base changes |
| `backend/tests/test_migration_integrity.py` | The integrity test itself |
| `backend/tests/test_fresh_install.py` | The fresh install test itself |
| `.github/workflows/ci.yml` | CI workflow changes |

If none of these paths changed, the DB-backed steps (fresh install + downgrade round-trip) are skipped to save CI minutes. The offline integrity check always runs regardless.
