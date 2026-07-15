---
name: echovault
description: Retrieve local-first project memory before substantive work and save only curated durable learnings.
---

# EchoVault curated memory

Behavior version: {{VERSION}}

Before substantive planning, debugging, architecture, or implementation, call `memory_context` with the current request and use the default 1,200-token context budget.
Use `memory_search` and `memory_details` only when the context pack is insufficient.
If `memory_context` reports that automatic context is off, continue without automatic search; explicit user-directed retrieval remains available.
Before the final response, call `memory_save` only for durable decisions, fixes, patterns, project state, or clarified requirements.

Generate one UUID per conceptual save and pass it as the `idempotency_key`. Treat `created`, `updated`, and `replayed` results as success. On a transient failure, retry once with the same idempotency key; if that retry fails, report the memory-save failure explicitly rather than claiming persistence.

Never store secrets, credentials, complete prompts, complete responses, or transcripts. Skip trivial or duplicate memories. Bound tool calls must not supply agent, source, or project identity fields; the MCP process assigns those authoritative values.

<!-- echovault:unbound-compatibility:start -->
## Unbound compatibility setup

When this skill is installed for a legacy unbound client, pass the client
identity explicitly:

```json
{
  "agent": "{{AGENT_NAME}}",
  "query": "<current user request>",
  "token_budget": 1200
}
```

CLI fallback:

```bash
memory context --project --agent {{AGENT_NAME}} --query "<current user request>"
```

The equivalent compact parameter spelling is `agent="{{AGENT_NAME}}"`.
<!-- echovault:unbound-compatibility:end -->
