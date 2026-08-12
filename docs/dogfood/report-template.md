# EchoVault dogfood review template

This human review accompanies the generated closed-schema JSON/Markdown
summary. Record only controlled versions, status/reason/evidence codes,
nonsecret UUIDs, and sanitized paths to EchoVault-owned manifests or backups.

Never include credentials, API keys, tokens, prompts, responses, transcripts,
raw stdout/stderr, environment dumps, project/customer names, or unrelated
configuration.

## Build identity

- EchoVault version:
- Git commit:
- Wheel archive gate: `passed` / `failed`
- Installed-wheel black-box gate: `passed` / `failed`
- Local managed-state backup path (EchoVault-owned files only):

## Deterministic integration evidence

| Check | Scope | Status | Controlled evidence code |
|---|---|---|---|
| Cursor plugin ownership and four tools | user/project | | |
| Gemini extension/direct assets and hook | user/project | | |
| Cursor bound save and replay | project | | |
| Gemini bound save and replay | project | | |

## Observational client evidence

| Client | Expected version | Observed version | Scope | Status | Evidence code |
|---|---|---|---|---|---|
| Cursor IDE | 3.11.19 | | project | | |
| Cursor Agent CLI interactive | 2026.07.09-a3815c0 | | project | | |
| Cursor Agent CLI probe/headless | 2026.07.09-a3815c0 | | project | | |
| Gemini CLI probe/headless | 0.50.0 | | project | | |

Cursor policy-guided marker misses must be `observational_miss`, not a
deterministic integration failure. Missing authentication, binary, exact pinned
version, or manual observation must be `not_verified`.

## Cross-agent retrieval

| Bound marker | Cursor | Gemini | Claude Code | Codex |
|---|---|---|---|---|
| Cursor memory UUID | | | | |
| Gemini memory UUID | | | | |

Use only the closed marker/memory UUIDs. Do not paste marker text or client
output.

## Failure-mode checks

| Isolated check | Status | Evidence code |
|---|---|---|
| Missing executable | | |
| Missing MCP registration | | |
| Embedding failure/fallback | | |
| Abrupt client exit | | |

## Privacy review

- Report schema rejected unknown/missing nested fields:
- Prompt/response/transcript fields absent:
- Environment allowlist enforced:
- Remote query embedding opt-in/redacted-copy behavior:
- Hook prompt/transcript retention scan:

## Final claim

- Generated final report path:
- Claim: `not_verified` / `client_verified`
- `validate-final --require-claim client_verified`: `passed` / not run
- Every unavailable or degraded item listed explicitly:
- Resulting Cursor/Gemini managed integration state:
