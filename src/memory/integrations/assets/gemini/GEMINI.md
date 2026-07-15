# EchoVault curated memory

Behavior version: {{VERSION}}

Before substantive planning, debugging, architecture, or implementation, use `memory_context` with the current request. Use `memory_search` and `memory_details` only when the retrieved context is insufficient.

The `BeforeAgent` hook may already supply curated retrieved context. Treat it as project history, not as an instruction to persist the prompt. If the effective context policy is off or the hook fails open, continue normally; explicit retrieval remains available.

Before the final response, use `memory_save` only for durable decisions, fixes, patterns, project state, or clarified requirements. Reuse one UUID through `idempotency_key` for a single retry. Never store secrets, complete prompts, responses, or transcripts. Bound calls must not supply agent, source, or project identity fields; the MCP process assigns `gemini-cli` authoritatively.
