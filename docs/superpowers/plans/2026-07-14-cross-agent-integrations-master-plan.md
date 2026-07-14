# EchoVault Cross-Agent Integrations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver production-ready curated memory integrations for Cursor IDE, Cursor CLI, and Gemini CLI on one hardened local EchoVault core.

**Architecture:** Implement the approved native-hybrid design in five sequential, independently reviewable phases. The local Markdown vault remains canonical, SQLite remains derived, MCP supplies shared tools, and client adapters own only installation, instructions, hooks, and diagnostics.

**Tech Stack:** Python 3.10–3.14, Click, MCP Python SDK, SQLite/FTS5/sqlite-vec, Markdown/JSON/TOML assets, pytest, uv, Cursor plugins, Gemini CLI extensions and hooks.

## Global Constraints

- Preserve unbound `memory mcp`, existing tool inputs, schema-v1 reads, and Claude Code/Codex/OpenCode setup behavior.
- Canonical agent IDs are `claude-code`, `codex`, `cursor`, `gemini-cli`, and `opencode`.
- Phase one resolves exactly one authoritative project per request. Multiple
  advertised roots require an explicit `cwd` contained by exactly one root;
  automatic multi-root routing and first-root fallbacks are excluded.
- Cursor task-start retrieval and curated saving are policy-guided; no test may describe model compliance as transactional.
- Gemini task-start retrieval is deterministic only when a supported `BeforeAgent` hook succeeds.
- Curated explicit `memory_save` is the only persistence trigger; no transcript ingestion or shutdown-save mining.
- Markdown schema v2 is canonical and lossless; SQLite, FTS, vectors, operation ledgers, and feedback counters are derived or operational.
- Automatic query embeddings remain local unless `context.allow_remote_query_embeddings: true`; remote queries receive only a redacted copy.
- Every single-file/config replacement is locked and atomic; multi-file or directory changes are journaled, recoverable, ownership-aware, and fail-closed on malformed or conflicting state.
- Keep the default context budget at approximately 1,200 tokens.
- Pin authenticated dogfood evidence separately to Cursor IDE 3.11.19, Cursor Agent CLI build 2026.07.09-a3815c0, and Gemini CLI 0.50.0.
- Begin every behavior change with an observed failing regression test; keep the 272-test baseline green between tasks.
- Never persist or print API keys, secrets, prompt bodies, transcripts, or unrelated environment values.

---

## Plan Set and Required Order

| Phase | Plan | Independently testable outcome | Depends on |
|---|---|---|---|
| 1 | [Local Core and Canonical Storage](2026-07-14-echovault-local-core-storage-plan.md) | Collision-safe project identity, schema-v2 canonical storage, deterministic merges, idempotency, locking, recovery, and migration | Approved design |
| 2 | [Bound MCP and Retrieval Privacy](2026-07-14-echovault-bound-mcp-plan.md) | Four project-scoped tools, MCP Roots, bound authority, details, aliases, and private query handling | Phase 1 |
| 3 | [Adapter Foundation and Cursor](2026-07-14-echovault-cursor-adapter-plan.md) | Safe adapter registry/config ownership plus Cursor global plugin and project fallback | Phases 1–2 |
| 4 | [Gemini Extension and Hooks](2026-07-14-echovault-gemini-adapter-plan.md) | Gemini native/direct modes, N/U/P state machine, idempotent `BeforeAgent`, and diagnostics | Phases 1–3 |
| 5 | [Packaging, CI, Documentation, and Dogfood](2026-07-14-echovault-release-gates-plan.md) | Wheel-complete assets, cross-platform gates, docs, smoke evidence, and verified local install | Phases 1–4 |

Do not execute phases in parallel. Within a phase, only tasks whose Interfaces blocks have no unmet producer may run concurrently. Each task ends in a green targeted suite and a focused commit.

## Reviewer Checkpoints

1. After Phase 1, review storage authority, crash recovery, schema migration, and all canonical-field merge rules before exposing them through MCP.
2. After Phase 2, review bound authority and project isolation with explicit cross-project negative tests.
3. After Phase 3, verify Cursor installation structurally and keep actual model compliance in the observational smoke report.
4. After Phase 4, run every N/U/P transition and the duplicate-hook event test before packaging.
5. After Phase 5, inspect wheel contents and the full CI matrix before locally replacing the installed EchoVault tool.

## Specification Traceability

| Approved design requirement | Owning plan and task |
|---|---|
| Canonical identities and collision-safe project keys | Phase 1, Tasks 2–3 |
| Legacy project alias adoption | Phase 1, Task 3 |
| Schema-v2 lossless Markdown and v1 enrichment | Phase 1, Tasks 4 and 10 |
| Deterministic duplicate merge and contributor provenance | Phase 1, Task 5 |
| Idempotent saves and operation history | Phase 1, Tasks 6–7 |
| Process locks, atomic/fsynced writes, recovery, reconciliation | Phase 1, Tasks 1 and 7–12 |
| Rust dashboard mutations through canonical Python persistence | Phase 1, Tasks 8–9 |
| Bound MCP identity/project authority and Roots | Phase 2, Tasks 2–4 |
| Project-scoped context/search/details/save | Phase 2, Tasks 1 and 3–5 |
| Remote query privacy and feedback suppression | Phase 2, Task 6 |
| Reusable adapter contract | Phase 3, Task 1 |
| Safe config mutation and ownership manifests | Phase 3, Tasks 2–3 |
| Cursor native plugin and project fallback | Phase 3, Tasks 4–8 |
| Cursor migration, uninstall, and doctor | Phase 3, Tasks 7–9 |
| Gemini extension/direct setup state machine | Phase 4, Tasks 1 and 5–7 |
| Gemini prompt hook and event claim | Phase 4, Tasks 2–4 |
| Gemini diagnostics and coexistence | Phase 4, Tasks 7–8 |
| Canonical packaged assets and wheel inspection | Phase 5, Tasks 1–3 |
| Ubuntu/macOS/Windows CI and validators | Phase 5, Tasks 2–4 |
| Backward compatibility, isolated-home integration, docs | Phase 5, Tasks 3 and 5 |
| Pinned authenticated smoke evidence and local dogfood | Phase 5, Tasks 6–7 |

## Completion Gate

The completion sections in all five phase artifacts must be satisfied before running the aggregate gate:

| Phase | Required gate file | Concrete evidence owned by that phase |
|---|---|---|
| 1 | `docs/superpowers/plans/2026-07-14-echovault-local-core-storage-plan.md` | Canonical schema-v2 vault, transaction recovery, reconciliation, migration, and Python/Rust bridge tests |
| 2 | `docs/superpowers/plans/2026-07-14-echovault-bound-mcp-plan.md` | Real-protocol unbound/bound MCP, Roots authority, project isolation, details, and privacy tests |
| 3 | `docs/superpowers/plans/2026-07-14-echovault-cursor-adapter-plan.md` | Cursor plugin/project assets, ownership, CLI, read-only doctor, and legacy compatibility tests |
| 4 | `docs/superpowers/plans/2026-07-14-echovault-gemini-adapter-plan.md` | Gemini N/U/P transitions, extension/direct assets, `BeforeAgent`, claim, CLI, and doctor tests |
| 5 | `docs/superpowers/plans/2026-07-14-echovault-release-gates-plan.md` | Archives, rendered bundles, installed-wheel verifier, CI definition, support docs, not_verified partial evidence, and strict final validation whenever a client-verified claim is made |

Run this self-contained aggregate gate from the repository root:

~~~bash
rm -rf dist build/validation build/wheel-venv
uv sync --extra dev
uv run --extra dev pytest -q
cargo test --manifest-path dashboard/Cargo.toml
uv build
uv run python scripts/verify_wheel_assets.py dist/*.whl dist/*.tar.gz
uv run python scripts/render_integration_fixture.py --client cursor --output build/validation/cursor --memory-command memory --version 0.6.0
uv run python scripts/validate_cursor_plugin.py build/validation/cursor
uv run python scripts/render_integration_fixture.py --client gemini --output build/validation/gemini --memory-command memory --version 0.6.0
uv venv build/wheel-venv --python 3.14
uv pip install --python build/wheel-venv/bin/python dist/*.whl
build/wheel-venv/bin/python scripts/verify_installed_tool.py --memory-executable build/wheel-venv/bin/memory
if test -f build/dogfood/client-verified-final.json; then
  uv run python scripts/smoke_clients.py validate-final --report build/dogfood/client-verified-final.json --require-claim client_verified
fi
git diff --check
git status --short
~~~

Expected: every command exits zero; `dist/`, `build/validation/cursor`, `build/validation/gemini`, and `build/wheel-venv` are recreated rather than reused; the working tree contains no unintended tracked changes; and CI confirms the pinned Gemini 0.50.0 validator and complete nine-cell OS/Python matrix. If Phase 5 created the canonical `build/dogfood/client-verified-final.json`, the aggregate gate strictly re-parses it and requires exactly the Cursor/Gemini bound-marker pair plus all pinned client cells under the `client_verified` claim. If a client was missing or unavailable, that file must not exist: the partial or not_verified report remains `not_verified`, the conditional validation is skipped, and no completion claim may call the clients verified. Policy-guided observations remain separate from deterministic success.

Publishing, pushing, marketplace submission, release tagging, and upstream pull requests remain separate user decisions after this gate.
